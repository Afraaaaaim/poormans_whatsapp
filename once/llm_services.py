"""
llm_services.py — LLM call service
=====================================
Location: once/llm_services.py

Pure LLM I/O only. No Redis, no history management, no DB.
History is the caller's responsibility (once.py manages it via RedisService).

Supports:
    - Cerebras as primary (fastest)
    - Groq as secondary
    - OpenRouter as last resort
    - Automatic rotation: when a provider hits rate limits or errors,
      all subsequent requests go to the next provider until it too is exhausted.

ENV VARS:
    CEREBRAS_API_KEY    — Cerebras API key
    CEREBRAS_MODEL      — model slug  (default: gpt-oss-120b)
    GROQ_API_KEY        — Groq API key
    GROQ_MODEL          — model slug  (default: llama3-8b-8192)
    OPENROUTER_API_KEY  — OpenRouter API key
    OPENROUTER_MODEL    — model slug  (default: openai/gpt-4o-mini)
    SYSTEM_PROMPT       — injected as system message on every call

Usage:
    from once.llm_services import LLMService

    reply = await LLMService.chat(messages=[
        {"role": "user", "content": "hello"},
    ])
"""

import asyncio
import os
from datetime import datetime

from cerebras.cloud.sdk import AsyncCerebras
from cerebras.cloud.sdk import RateLimitError as CerebrasRateLimitError
from openai import APIError, AsyncOpenAI

from once.logger import get_logger, new_span

log = get_logger(__name__)

# ── ENV ───────────────────────────────────────────────────────────────────────
CEREBRAS_API_KEY  = os.getenv("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL    = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")
GROQ_API_KEY      = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL        = os.getenv("GROQ_MODEL", "llama3-8b-8192")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL  = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a personal assistant. Be concise — replies go over WhatsApp.",
)

# ── Clients — created once at import time ─────────────────────────────────────
_cerebras = AsyncCerebras(
    api_key=CEREBRAS_API_KEY,
    timeout=5,
    max_retries=0,  # we handle retries via rotation, not per-client
    warm_tcp_connection=True,
)

_groq = AsyncOpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
    timeout=5,
    max_retries=0,
)

_openrouter = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
    timeout=20,
    max_retries=1,
)

# ── Provider rotation state ───────────────────────────────────────────────────
# Index into _PROVIDERS. Starts at 0 (Cerebras). Rotates forward on exhaustion.
# Uses a lock so concurrent async requests don't race on the index.

_PROVIDERS = ["cerebras", "groq", "openrouter"]
_current_provider_index = 0
_provider_lock = asyncio.Lock()


async def _get_provider() -> str:
    return _PROVIDERS[_current_provider_index]


async def _rotate_provider(failed_provider: str) -> str | None:
    """
    Rotate to the next provider if the failed one is still current.
    Returns the new provider name, or None if all providers are exhausted.
    """
    global _current_provider_index
    async with _provider_lock:
        current = _PROVIDERS[_current_provider_index]
        # Only rotate if the failed provider is still the active one
        # (another coroutine may have already rotated)
        if current == failed_provider:
            _current_provider_index += 1
            if _current_provider_index >= len(_PROVIDERS):
                log.error("All LLM providers exhausted")
                _current_provider_index= 0
            new = _PROVIDERS[_current_provider_index]
            log.warning("Rotated LLM provider: %s → %s", failed_provider, new)
            return new
        return current  # already rotated by another coroutine


# ── Internal call helpers ─────────────────────────────────────────────────────

async def _call_cerebras(messages: list[dict]) -> str:
    completion = await _cerebras.chat.completions.create(
        model=CEREBRAS_MODEL,
        messages=messages,
        max_completion_tokens=1000,
        temperature=0.7,
        stream=False,
    )
    return completion.choices[0].message.content


async def _call_openai_compat(client: AsyncOpenAI, model: str, messages: list[dict], is_openrouter: bool = False) -> str:
    kwargs = dict(
        model=model,
        messages=messages,
        stream=True,
        max_completion_tokens=1000,
        temperature=0.7,
    )
    if is_openrouter:
        kwargs["extra_body"] = {"provider": {"sort": "throughput"}}

    stream = await client.chat.completions.create(**kwargs)
    reply = ""
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content
        if delta:
            reply += delta
    return reply


# ── LLMService ────────────────────────────────────────────────────────────────

class LLMService:
    """
    Stateless LLM service with automatic provider rotation.

    Provider priority: Cerebras → Groq → OpenRouter
    When a provider hits a rate limit or any API error, all subsequent
    requests rotate to the next provider automatically.
    """

    @staticmethod
    async def chat(messages: list[dict], system_prompt: str | None = None) -> str:
        with new_span("llm.chat"):
            full_messages = [{"role": "system", "content": system_prompt or SYSTEM_PROMPT}] + messages
            started = datetime.now()

            while True:
                provider = await _get_provider()

                try:
                    with new_span(f"llm.{provider}"):
                        log.debug("Calling provider=%s", provider)

                        if provider == "cerebras":
                            reply = await _call_cerebras(full_messages)

                        elif provider == "groq":
                            reply = await _call_openai_compat(_groq, GROQ_MODEL, full_messages)

                        elif provider == "openrouter":
                            reply = await _call_openai_compat(_openrouter, OPENROUTER_MODEL, full_messages, is_openrouter=True)

                        else:
                            raise RuntimeError(f"Unknown provider: {provider}")

                        elapsed = (datetime.now() - started).total_seconds()
                        log.success(
                            "[%s] replied in %.2fs (%d chars)",
                            provider, elapsed, len(reply),
                        )
                        return reply

                except CerebrasRateLimitError as e:
                    log.warning("[cerebras] rate limited — rotating: %s", e)
                    next_p = await _rotate_provider("cerebras")
                    if next_p is None:
                        raise

                except APIError as e:
                    status = getattr(e, "status_code", None)
                    if status == 429:
                        log.warning("[%s] 429 rate limited — rotating", provider)
                        next_p = await _rotate_provider(provider)
                        if next_p is None:
                            raise
                    else:
                        log.exception("[%s] API error (non-429): %s", provider, e)
                        raise

                except Exception as e:
                    log.exception("[%s] unexpected error — rotating: %s", provider, e)
                    next_p = await _rotate_provider(provider)
                    if next_p is None:
                        raise