"""
llm_services.py — LLM call service
=====================================
Location: once/llm_services.py

Pure LLM I/O only. No Redis, no history management, no DB.
History is the caller's responsibility (once.py manages it via RedisService).

Supports:
    - OpenRouter as primary
    - Groq as fallback on 429

ENV VARS:
    OPENROUTER_API_KEY  — OpenRouter API key
    OPENROUTER_MODEL    — model slug  (default: openai/gpt-4o-mini)
    GROQ_API_KEY        — Groq API key
    GROQ_MODEL          — model slug  (default: llama3-8b-8192)
    SYSTEM_PROMPT       — injected as system message on every call

Usage:
    from once.llm_services import LLMService

    reply = await LLMService.chat(messages=[
        {"role": "user", "content": "hello"},
    ])
"""

import os
from datetime import datetime

from openai import APIError, AsyncOpenAI

from once.logger import get_logger, new_span

log = get_logger(__name__)

# ── ENV ───────────────────────────────────────────────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama3-8b-8192")

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a personal assistant. Be concise — replies go over WhatsApp.",
)

# ── Clients — created once at import time ─────────────────────────────────────
_openrouter = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
)

_groq = AsyncOpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
)


# ── Internal ──────────────────────────────────────────────────────────────────


async def _stream(client: AsyncOpenAI, model: str, messages: list[dict]) -> str:
    """Stream a completion and return the full assembled text."""
    is_groq = "groq.com" in str(client.base_url)

    kwargs = dict(
        model=model,
        messages=messages,
        stream=True,
        max_tokens=300,
        temperature=0.7,
    )
    if not is_groq:
        kwargs["extra_body"] = {"provider": {"sort": "throughput"}}

    stream = await client.chat.completions.create(**kwargs)
    reply = ""
    async for chunk in stream:
        if not chunk.choices:
            continue  # skip empty chunks (usage stats, [DONE], etc.)
        delta = chunk.choices[0].delta.content
        if delta:
            reply += delta
    return reply


# ── LLMService ────────────────────────────────────────────────────────────────


class LLMService:
    """
    Stateless LLM service. Call directly, no instantiation.

    Caller is responsible for:
        - Building the messages list (history + new user message)
        - Saving the reply back to history after the call

    This class only handles: send → stream → return reply text.
    """

    @staticmethod
    async def chat(messages: list[dict]) -> str:
        """
        Send a message list to the LLM and return the assistant's reply.

        messages — full OpenAI-format list, e.g.:
            [
                {"role": "user", "content": "what time is it?"},
            ]
        The system prompt is prepended automatically from ENV.

        Tries OpenRouter first. Falls back to Groq on 429.
        Raises on all other errors so the caller can handle them.
        """
        with new_span("llm.chat"):
            full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
            started = datetime.now()

            # ── Primary: OpenRouter ──────────────────────────────────────────
            try:
                with new_span("llm.openrouter"):
                    log.debug(
                        "Calling OpenRouter model=%s msgs=%d",
                        OPENROUTER_MODEL,
                        len(full_messages),
                    )
                    reply = await _stream(_openrouter, OPENROUTER_MODEL, full_messages)
                    elapsed = (datetime.now() - started).total_seconds()
                    log.success(
                        "OpenRouter replied in %.2fs (%d chars)", elapsed, len(reply)
                    )
                    return reply

            except APIError as e:
                if getattr(e, "status_code", None) != 429:
                    log.exception("OpenRouter API error (non-429): %s", e)
                    raise
                log.warning("OpenRouter 429 — falling back to Groq")

            # ── Fallback: Groq ───────────────────────────────────────────────
            with new_span("llm.groq_fallback"):
                log.debug("Calling Groq model=%s", GROQ_MODEL)
                try:
                    reply = await _stream(_groq, GROQ_MODEL, full_messages)
                    elapsed = (datetime.now() - started).total_seconds()
                    log.success(
                        "Groq fallback replied in %.2fs (%d chars)", elapsed, len(reply)
                    )
                    return reply
                except Exception:
                    log.exception("Groq fallback also failed")
                    raise
