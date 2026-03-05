"""
redis_service.py — Central Redis service
==========================================
Location: once/redis_service.py

ENV VARS:
    REDIS_URL            — redis://redis:6379/0  (default)
    HISTORY_MAX_PAIRS    — max back-and-forth pairs to keep (default: 10)
    HISTORY_TTL_SECONDS  — history TTL in seconds (default: 86400 = 24h)
    CACHE_TTL_SECONDS    — default TTL for generic cache entries (default: 300 = 5min)
"""

import asyncio
import json
import os

import redis.asyncio as aioredis

from once.logger import get_logger, new_span
from once.constants import HISTORY_NS, CACHE_NS
log = get_logger(__name__)

REDIS_URL = os.getenv("REDIS_URL")
HISTORY_MAX_PAIRS = int(os.getenv("HISTORY_MAX_PAIRS"))
HISTORY_TTL = int(os.getenv("HISTORY_TTL_SECONDS"))
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS"))
DEDUP_TTL = int(os.getenv("DEDUP_TTL_SECONDS"))  # 8 days in seconds, longer than WhatsApp's 7-day dedup window
_local_dedup: set[str] = set()


def _history_key(phone: str) -> str:
    return f"{HISTORY_NS}:{phone}"


def _cache_key(key: str) -> str:
    return f"{CACHE_NS}:{key}"


_client: aioredis.Redis = aioredis.from_url(REDIS_URL, decode_responses=True)


class RedisService:

    # ── HISTORY ───────────────────────────────────────────────────────────────

    @staticmethod
    async def get_history(phone: str) -> list[dict]:
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
        with new_span("redis.save_history"):
            max_messages = HISTORY_MAX_PAIRS * 2
            if len(history) > max_messages:
                history = history[-max_messages:]
                log.debug("Trimmed history to %d messages for %s", max_messages, phone)
            await _client.set(
                _history_key(phone),
                json.dumps(history),
                ex=HISTORY_TTL,
            )
            log.debug("Saved %d history entries for %s (TTL=%ds)", len(history), phone, HISTORY_TTL)

    @staticmethod
    async def clear_history(phone: str) -> None:
        with new_span("redis.clear_history"):
            await _client.delete(_history_key(phone))
            log.info("Cleared history for %s", phone)

    @staticmethod
    async def refresh_history_ttl(phone: str) -> None:
        await _client.expire(_history_key(phone), HISTORY_TTL)
        log.debug("Refreshed TTL for %s to %ds", phone, HISTORY_TTL)

    @staticmethod
    async def get_history_ttl(phone: str) -> int:
        return await _client.ttl(_history_key(phone))

    # ── GENERIC CACHE ─────────────────────────────────────────────────────────

    @staticmethod
    async def cache_set(key: str, value: str, ttl: int = CACHE_TTL) -> None:
        await _client.set(_cache_key(key), value, ex=ttl)
        log.debug("Cache set: %s (TTL=%ds)", key, ttl)

    @staticmethod
    async def cache_get(key: str) -> str | None:
        value = await _client.get(_cache_key(key))
        log.debug("Cache get: %s → %s", key, "hit" if value is not None else "miss")
        return value

    @staticmethod
    async def cache_delete(key: str) -> None:
        await _client.delete(_cache_key(key))
        log.debug("Cache deleted: %s", key)

    @staticmethod
    async def cache_increment(key: str, ttl: int = CACHE_TTL) -> int:
        pipe = _client.pipeline()
        full_key = _cache_key(key)
        await pipe.incr(full_key)
        await pipe.expire(full_key, ttl, xx=False)
        results = await pipe.execute()
        count = results[0]
        log.debug("Cache increment: %s → %d", key, count)
        return count

    # ── DEDUP ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def is_duplicate_message(waba_message_id: str, ttl: int = DEDUP_TTL) -> bool:
        key = f"dedup:{waba_message_id}"
        try:
            is_new = await _client.set(key, "1", ex=ttl, nx=True)
            return is_new is None
        except Exception:
            log.warning("Redis unavailable for dedup — using in-memory fallback")
            if waba_message_id in _local_dedup:
                return True
            _local_dedup.add(waba_message_id)
            if len(_local_dedup) > 1000:
                _local_dedup.clear()
            return False

    # ── HEALTH CHECK ──────────────────────────────────────────────────────────

    @staticmethod
    async def ping() -> bool:
        try:
            await _client.ping()
            log.debug("Redis ping: OK")
            return True
        except Exception:
            log.error("Redis ping: FAILED")
            return False

    @staticmethod
    def sync_ping() -> bool:
        """Sync wrapper for ping. Use only at startup."""
        return asyncio.run(RedisService.ping())