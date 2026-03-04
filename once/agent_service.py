"""
once/agent_service.py

Two-tier LLM agent loop — powered by OpenAI Agents SDK + FastMCP SSE client.

Flow:
  LLM 1 (Cerebras) emits ##AGENT::<reason>::<summary>## handoff signal
        ↓
  agent_run() connects to mcpserver via SSE, runs agentic loop
        ↓
  Agent (DeepSeek V3 via OpenRouter) has ALL mcpserver tools available
  SDK handles tool_choice, retries, and loop automatically
        ↓
  Returns final result string → LLM 1 wraps it for the user

WhatsApp step updates:
  🔍 On it! Let me figure out the right move...
  ✅ Done! Here's what happened...
  ⚠️  Hit a snag, but handled it.
"""

from __future__ import annotations

import os
from typing import Any

from openai import AsyncOpenAI
from agents import Agent, Runner, RunConfig, OpenAIChatCompletionsModel, set_tracing_disabled
from agents.mcp import MCPServerSse

from once.logger import get_logger, new_span
from once.helper_functions import send_whatsapp_reply

log = get_logger(__name__)

# Disable openai-agents SDK tracing — we use our own logger
set_tracing_disabled(True)

# ── Config ────────────────────────────────────────────────────────────────────

AGENT_MODEL    = os.getenv("AGENT_MODEL", "deepseek/deepseek-chat")
MCP_SERVER_URL = os.getenv("MCP_BASE_URL", "http://mcp-server:8090/sse")
MAX_TURNS      = int(os.getenv("AGENT_MAX_TURNS", "10"))

AGENT_SYSTEM_PROMPT = (
    "You are a precise task-execution agent for a WhatsApp assistant. "
    "You have access to tools. Use them to complete the task. "
    "Always pass caller_role when a tool requires it. "
    "Be concise. Do not explain — act and return results."
    "If a parameter is missing or not provided. prompt the user about it."
    "Auto correct is fine but prompt the user with the possible right answer"
    "Do not assume any empty values."
)

# ── OpenRouter client for the agent ──────────────────────────────────────────
# Separate from LLMService's _openrouter client — agent needs longer timeout.

_agent_openai_client = AsyncOpenAI(
    api_key=os.getenv("OPENROUTER_API_KEY", ""),
    base_url="https://openrouter.ai/api/v1",
    timeout=60,
    max_retries=1,
    default_headers={
        "HTTP-Referer": "https://github.com/poormans-whatsapp",
        "X-Title": "poormans_whatsapp_agent",
    },
)

# ── WA step update ────────────────────────────────────────────────────────────

async def _wa_update(wa: Any, user_phone: str, message: str) -> None:
    """Fire-and-forget WhatsApp step update — swallows errors."""
    try:
        await send_whatsapp_reply(wa, user_phone, message)
    except Exception:
        log.exception("WA step update failed for %s", user_phone)


# ── Agent runner ──────────────────────────────────────────────────────────────

async def agent_run(
    *,
    wa: Any,
    reason: str,
    compressed_summary: str,
    user_phone: str,
    caller_role: str,
) -> str:
    """
    Run the agentic loop and return a plain-text result for LLM 1.

    Args:
        wa:                 WhatsApp client instance (from once.py).
        reason:             Why the agent was invoked (from LLM 1).
        compressed_summary: Short conversation context (from LLM 1).
        user_phone:         E.164 digits-only phone for WA updates.
        caller_role:        User's role — passed to every tool call.

    Returns:
        Plain-text result string for LLM 1 to incorporate in its reply.
    """
    with new_span("agent.run"):
        log.info(
            "Agent started | reason=%s | role=%s | phone=%s",
            reason, caller_role, user_phone,
        )

        await _wa_update(wa, user_phone, "🔍 On it! Let me figure out the right move...")

        task = (
            f"Task: {reason}\n\n"
            f"Context: {compressed_summary}\n\n"
            f"caller_role: {caller_role}\n"
            "Use the available tools to complete this task. "
            f"Always pass caller_role='{caller_role}' when tools require it."
        )

        mcp_server = MCPServerSse(
            {"url": MCP_SERVER_URL},
            name="mcpserver",
            cache_tools_list=True,
        )

        try:
            with new_span("agent.mcp_connect"):
                log.debug("Connecting to MCP server at %s", MCP_SERVER_URL)
                await mcp_server.connect()
                log.info("MCP server connected | url=%s", MCP_SERVER_URL)

            agent = Agent(
                name="task_agent",
                instructions=AGENT_SYSTEM_PROMPT,
                mcp_servers=[mcp_server],
                model=OpenAIChatCompletionsModel(
                    model=AGENT_MODEL,
                    openai_client=_agent_openai_client,
                ),
            )

            run_config = RunConfig(tracing_disabled=True)

            with new_span("agent.loop"):
                log.debug(
                    "Starting agent loop | model=%s | max_turns=%d",
                    AGENT_MODEL, MAX_TURNS,
                )
                result = await Runner.run(
                    agent,
                    task,
                    run_config=run_config,
                    max_turns=MAX_TURNS,
                )

            final = result.final_output or "Task completed with no output."
            log.success(
                "Agent finished | output_len=%d | output_preview=%s",
                len(final),
                final[:100],
            )
            await _wa_update(wa, user_phone, "✅ Done! Here's what happened...")
            return final

        except Exception as exc:
            log.exception("Agent loop failed | reason=%s | error=%s", reason, exc)
            await _wa_update(wa, user_phone, "⚠️ Hit a snag, but handled it.")
            return f"Agent encountered an error: {exc}"

        finally:
            with new_span("agent.mcp_cleanup"):
                try:
                    await mcp_server.cleanup()
                    log.debug("MCP server connection cleaned up")
                except Exception:
                    log.warning("MCP cleanup failed — ignored")