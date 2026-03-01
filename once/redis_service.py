"""
redis_service.py — Central Redis service
==========================================
Location: once/redis_service.py

Single source of truth for everything Redis in this project.
All other files import from here — never create a Redis client elsewhere.

Responsibilities:
    - Singleton async Redis client
    - Chat history management (get, save, clear, list all)
    - TTL management
    - Generic cache (get/set/delete) for future use (auth cache, rate limits, etc.)
    - Sync wrappers at the bottom for Celery tasks (which are sync)

ENV VARS:
    REDIS_URL            — redis://redis:6379/0  (default)
    HISTORY_MAX_PAIRS    — max back-and-forth pairs to keep in history (default: 10)
    HISTORY_TTL_SECONDS  — how long history lives in Redis before expiry (default: 86400 = 24h)
    CACHE_TTL_SECONDS    — default TTL for generic cache entries (default: 300 = 5min)

Usage (async):
    from once.redis_service import RedisService

    # History
    history = await RedisService.get_history("+91...")
    await RedisService.save_history("+91...", history)
    await RedisService.clear_history("+91...")
    phones = await RedisService.get_all_history_phones()

    # Generic cache
    await RedisService.cache_set("auth:+91...", "1", ttl=60)
    value = await RedisService.cache_get("auth:+91...")
    await RedisService.cache_delete("auth:+91...")

Usage (sync — from Celery tasks only):
    from once.redis_service import RedisService

    history = RedisService.sync_get_history("+91...")
    phones  = RedisService.sync_get_all_history_phones()
"""

import asyncio
import json
import os

import redis.asyncio as aioredis

from once.logger import get_logger, new_span

log = get_logger(__name__)

# ── ENV ───────────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
HISTORY_MAX_PAIRS = int(os.getenv("HISTORY_MAX_PAIRS", "10"))
HISTORY_TTL = int(os.getenv("HISTORY_TTL_SECONDS", "86400"))  # 24h
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "300"))  # 5min

# ── Key namespaces — change these if you ever need to prefix by env ───────────
_HISTORY_NS = "chat_history"  # chat_history:+919562885142
_CACHE_NS = "cache"  # cache:auth:+919562885142


def _history_key(phone: str) -> str:
    return f"{_HISTORY_NS}:{phone}"


def _cache_key(key: str) -> str:
    return f"{_CACHE_NS}:{key}"


# ── Singleton client ──────────────────────────────────────────────────────────
# Created once at import time. All methods share this single connection pool.
_client: aioredis.Redis = aioredis.from_url(REDIS_URL, decode_responses=True)
_local_dedup: set[str] = set()


# ── RedisService ──────────────────────────────────────────────────────────────


class RedisService:
    """
    Stateless Redis service. All methods are static.
    Async methods for use in FastAPI/async code.
    Sync methods (sync_*) for use in Celery tasks only.

    Never instantiate — call directly:
        history = await RedisService.get_history("+91...")
    """

    # ── HISTORY ───────────────────────────────────────────────────────────────

    @staticmethod
    async def get_history(phone: str) -> list[dict]:
        """
        Load conversation history for a phone number from Redis.
        Returns an empty list if no history exists or if data is corrupt.

        History format (OpenAI-compatible):
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
                ...
            ]
        """
        with new_span("redis.get_history"):
            raw = await _client.get(_history_key(phone))
            if not raw:
                log.debug("No history in Redis for %s", phone)
                return []
            try:
                history = json.loads(raw)
                log.debug("Loaded %d history entries for %s", len(history), phone)
                return history
            except json.JSONDecodeError:
                log.warning("Corrupt history for %s — resetting to empty", phone)
                await _client.delete(_history_key(phone))
                return []

    @staticmethod
    async def save_history(phone: str, history: list[dict]) -> None:
        """
        Persist conversation history to Redis with TTL.
        Automatically trims to HISTORY_MAX_PAIRS before saving
        so Redis never grows unbounded.

        Call this after every LLM exchange.
        """
        with new_span("redis.save_history"):
            max_messages = HISTORY_MAX_PAIRS * 2  # pairs = user + assistant
            if len(history) > max_messages:
                history = history[-max_messages:]
                log.debug("Trimmed history to %d messages for %s", max_messages, phone)

            await _client.set(
                _history_key(phone),
                json.dumps(history),
                ex=HISTORY_TTL,
            )
            log.debug(
                "Saved %d history entries for %s (TTL=%ds)",
                len(history),
                phone,
                HISTORY_TTL,
            )

    @staticmethod
    async def append_to_history(phone: str, role: str, content: str) -> list[dict]:
        """
        Convenience method — load history, append one entry, save, return updated list.
        Use this to avoid load/modify/save boilerplate in once.py.

        role: 'user' or 'assistant'
        """
        history = await RedisService.get_history(phone)
        history.append({"role": role, "content": content})
        await RedisService.save_history(phone, history)
        return history

    @staticmethod
    async def clear_history(phone: str) -> None:
        """
        Delete a user's history from Redis entirely.
        Use this to reset the LLM's memory for a user
        (e.g. if they send a 'reset' command).
        """
        with new_span("redis.clear_history"):
            await _client.delete(_history_key(phone))
            log.info("Cleared history for %s", phone)

    @staticmethod
    async def get_all_history_phones() -> list[str]:
        """
        Return all phone numbers that currently have active history in Redis.
        Used by the Celery flush task to iterate over all active conversations.

        Returns a list of E.164 phone number strings (prefix stripped).
        """
        with new_span("redis.get_all_history_phones"):
            keys = await _client.keys(f"{_HISTORY_NS}:*")
            prefix = f"{_HISTORY_NS}:"
            phones = [k[len(prefix) :] for k in keys]
            log.debug("Found %d active history keys in Redis", len(phones))
            return phones

    @staticmethod
    async def get_history_ttl(phone: str) -> int:
        """
        Returns the remaining TTL in seconds for a phone's history.
        Returns -1 if the key has no TTL, -2 if the key doesn't exist.
        Useful for debugging or deciding whether to extend TTL.
        """
        return await _client.ttl(_history_key(phone))

    @staticmethod
    async def refresh_history_ttl(phone: str) -> None:
        """
        Reset the TTL on a phone's history back to HISTORY_TTL.
        Call this on every interaction so active users never expire.
        """
        await _client.expire(_history_key(phone), HISTORY_TTL)
        log.debug("Refreshed TTL for %s to %ds", phone, HISTORY_TTL)

    # ── GENERIC CACHE ─────────────────────────────────────────────────────────
    # Use these for anything that isn't conversation history:
    # auth results, rate limit counters, any short-lived computed values.

    @staticmethod
    async def cache_set(key: str, value: str, ttl: int = CACHE_TTL) -> None:
        """
        Store a string value in the generic cache namespace.
        key    — logical key, e.g. 'auth:+919562885142'
        value  — must be a string (JSON-encode complex values before passing)
        ttl    — seconds until expiry (default: CACHE_TTL env var, 5min)

        Example:
            await RedisService.cache_set("auth:+91...", "1", ttl=60)
        """
        await _client.set(_cache_key(key), value, ex=ttl)
        log.debug("Cache set: %s (TTL=%ds)", key, ttl)

    @staticmethod
    async def cache_get(key: str) -> str | None:
        """
        Retrieve a value from the generic cache.
        Returns None if the key doesn't exist or has expired.

        Example:
            val = await RedisService.cache_get("auth:+91...")
            if val is None:
                # cache miss — do the real lookup
        """
        value = await _client.get(_cache_key(key))
        log.debug("Cache get: %s → %s", key, "hit" if value is not None else "miss")
        return value

    @staticmethod
    async def cache_delete(key: str) -> None:
        """Remove a key from the generic cache immediately."""
        await _client.delete(_cache_key(key))
        log.debug("Cache deleted: %s", key)

    @staticmethod
    async def cache_increment(key: str, ttl: int = CACHE_TTL) -> int:
        """
        Atomically increment an integer counter in the cache.
        Creates the key with value 1 if it doesn't exist.
        Sets TTL only on creation (preserves existing TTL on subsequent increments).
        Useful for rate limiting.

        Example:
            count = await RedisService.cache_increment("rate:+91...")
            if count > 10:
                # rate limited
        """
        pipe = _client.pipeline()
        full_key = _cache_key(key)
        await pipe.incr(full_key)
        await pipe.expire(full_key, ttl, xx=False)  # xx=False: only set if not exists
        results = await pipe.execute()
        count = results[0]
        log.debug("Cache increment: %s → %d", key, count)
        return count

    # ── HEALTH CHECK ──────────────────────────────────────────────────────────

    @staticmethod
    async def ping() -> bool:
        """
        Check if Redis is reachable.
        Returns True if healthy, False if not.
        Call on app startup to catch misconfig early.
        """
        try:
            await _client.ping()
            log.debug("Redis ping: OK")
            return True
        except Exception:
            log.error("Redis ping: FAILED")
            return False

    # ── SYNC WRAPPERS (Celery tasks only) ─────────────────────────────────────
    # Celery workers are synchronous. These wrappers run the async methods
    # in a fresh event loop so tasks.py never needs to know about asyncio.
    #
    # Rule: only call these from tasks.py. Never call them from async code.

    @staticmethod
    def sync_get_history(phone: str) -> list[dict]:
        """Sync wrapper for get_history. Use only in Celery tasks."""
        return asyncio.run(RedisService.get_history(phone))

    @staticmethod
    def sync_get_all_history_phones() -> list[str]:
        """Sync wrapper for get_all_history_phones. Use only in Celery tasks."""
        return asyncio.run(RedisService.get_all_history_phones())

    @staticmethod
    def sync_ping() -> bool:
        """Sync wrapper for ping. Use only in Celery tasks or startup scripts."""
        return asyncio.run(RedisService.ping())

    # Module-level fallback for when Redis is unavailable

    @staticmethod
    async def is_duplicate_message(waba_message_id: str, ttl: int = 86400) -> bool:
        """
        Returns True if duplicate. Falls back to in-memory set if Redis is down.
        """
        key = f"dedup:{waba_message_id}"
        try:
            is_new = await _client.set(key, "1", ex=ttl, nx=True)
            return is_new is None
        except Exception:
            log.warning("Redis unavailable for dedup — using in-memory fallback")
            if waba_message_id in _local_dedup:
                return True
            _local_dedup.add(waba_message_id)
            # Prevent unbounded growth
            if len(_local_dedup) > 1000:
                _local_dedup.clear()
            return False
