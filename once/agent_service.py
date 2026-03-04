"""
once/agent_service.py

Agentic loop — powered by CrewAI + FastMCP SSE.

Flow:
  LLM1 (Cerebras) decides an action is needed
        ↓
  agent_run() spins up a CrewAI crew with MCPServerAdapter
  pointing at your existing MCP SSE server — all tools auto-discovered
        ↓
  Single task-execution agent (DeepSeek V3 via OpenRouter) runs the task
        ↓
  Returns final result string → LLM1 wraps it for the user

WhatsApp step updates:
  🔍 On it! Let me figure out the right move...
  ✅ Done! Here's what happened...
  ⚠️  Hit a snag, but handled it.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from crewai import Agent, Crew, Task, Process
from crewai_tools import MCPServerAdapter

from once.logger import get_logger, new_span
from once.helper_functions import send_whatsapp_reply

log = get_logger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

AGENT_MODEL    = os.getenv("AGENT_MODEL", "deepseek/deepseek-chat")
MCP_SERVER_URL = os.getenv("MCP_BASE_URL", "http://mcp-server:8090/sse")
MAX_TURNS      = int(os.getenv("AGENT_MAX_TURNS", "10"))

AGENT_ROLE      = "Task Execution Agent"
AGENT_GOAL      = "Complete the requested task accurately using available tools."
AGENT_BACKSTORY = (
    "You are a precise task-execution agent for a WhatsApp assistant. "
    "You MUST call a tool for every task -- never reason about or guess the outcome. "
    "If the task involves a user (activate, deactivate, add, remove, search), "
    "call the appropriate tool immediately with whatever information you have. "
    "A name alone is sufficient to identify a user -- do NOT ask for a phone number "
    "before calling the tool. The tool handles user lookup internally. "
    "Always pass caller_role when a tool requires it. "
    "Never fabricate a result. If you did not call a tool, you do not have a result. "
    "Be concise -- act and return the tool result directly."
)

# ── Raw OpenAI-compatible client pointing at OpenRouter ──────────────────────
# Bypasses CrewAI's LLM() wrapper entirely — no LiteLLM dependency needed.
# CrewAI Agent accepts a plain string model name; we override the underlying
# client via OPENAI_API_BASE + OPENAI_API_KEY env vars which the openai SDK
# reads automatically. Set them here so they're available before any import.

os.environ["OPENAI_API_BASE"] = "https://openrouter.ai/api/v1"
os.environ["OPENAI_API_KEY"]  = os.getenv("OPENROUTER_API_KEY", "")

# ── WA step update ────────────────────────────────────────────────────────────

async def _wa_update(wa: Any, user_phone: str, message: str) -> None:
    """Fire-and-forget WhatsApp step update — swallows errors."""
    try:
        await send_whatsapp_reply(wa, user_phone, message)
    except Exception:
        log.exception("WA step update failed for %s", user_phone)


# ── Crew builder (sync — called inside asyncio.to_thread) ────────────────────

def _run_crew(task_description: str, caller_role: str) -> str:
    """
    Builds and kicks off a CrewAI crew synchronously.
    MCPServerAdapter auto-discovers all tools from the SSE server.
    Wrapped in asyncio.to_thread by the async caller.
    """
    server_params = {"url": MCP_SERVER_URL}

    with MCPServerAdapter(server_params) as mcp_tools:
        log.info("MCP tools discovered: %s", [t.name for t in mcp_tools])

        agent = Agent(
            role=AGENT_ROLE,
            goal=AGENT_GOAL,
            backstory=AGENT_BACKSTORY,
            tools=mcp_tools,
            model=AGENT_MODEL,
            max_iter=MAX_TURNS,
            verbose=False,
        )

        task = Task(
            description=(
                f"{task_description}\n\n"
                f"caller_role: {caller_role}\n"
                f"Always pass caller_role='{caller_role}' when tools require it."
            ),
            expected_output="A concise plain-text summary of what was done and the result.",
            agent=agent,
        )

        crew = Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            verbose=False,
        )

        result = crew.kickoff()
        return str(result)


# ── Public entry point ────────────────────────────────────────────────────────

async def agent_run(
    *,
    wa: Any,
    reason: str,
    compressed_summary: str,
    user_phone: str,
    caller_role: str,
) -> str:
    """
    Run the agentic loop and return a plain-text result for LLM1.

    Args:
        wa:                 WhatsApp client instance (from once.py).
        reason:             Why the agent was invoked (from LLM1).
        compressed_summary: Short conversation context (from LLM1).
        user_phone:         E.164 digits-only phone for WA updates.
        caller_role:        User's role — passed to every tool call.

    Returns:
        Plain-text result string for LLM1 to incorporate in its reply.
    """
    with new_span("agent.run"):
        log.info(
            "Agent started | reason=%s | role=%s | phone=%s",
            reason, caller_role, user_phone,
        )

        await _wa_update(wa, user_phone, "🔍 On it! Let me figure out the right move...")

        task_description = (
            f"Task: {reason}\n\n"
            f"Context: {compressed_summary}"
        )

        try:
            with new_span("agent.crew_run"):
                final = await asyncio.to_thread(
                    _run_crew,
                    task_description,
                    caller_role,
                )

            log.success(
                "Agent finished | output_len=%d | preview=%s",
                len(final), final[:100],
            )
            await _wa_update(wa, user_phone, "✅ Done! Here's what happened...")
            print("#####################################")
            print(str(final))
            print("#####################################")

            return final

        except Exception as exc:
            log.exception("Agent loop failed | reason=%s | error=%s", reason, exc)
            await _wa_update(wa, user_phone, "⚠️ Hit a snag, but handled it.")
            return f"Agent encountered an error: {exc}"