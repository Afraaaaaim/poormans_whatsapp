"""
helper_function.py — Helpers for once.py orchestration
=======================================================
Location: once/helper_function.py

Controls whether Redis is used as a write-behind cache via:
    USE_REDIS_CACHE=true  — Redis-first, DB flushed by Celery
    USE_REDIS_CACHE=false — Direct DB on every operation (default)

Redis key layout:
    cache:user:{phone}           — serialized user dict
    cache:conversation:default   — serialized conversation dict
    queue:inbound_saves          — pending inbound message dicts
    queue:outbound_saves         — pending outbound message dicts
    queue:status_updates         — pending status update dicts
    queue:waba_patches           — pending waba_id patch dicts
    msg:{temp_id}                — outbound message dict (for status lookups)
    msg:waba:{waba_id}           — maps waba_id → temp_id
"""

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

from once.db_services import DBService
from once.logger import get_logger, new_span
from once.redis_service import RedisService, _client

log = get_logger(__name__)

USE_REDIS_CACHE = os.getenv("USE_REDIS_CACHE", "false").lower() == "true"

_USER_CACHE_TTL = int(os.getenv("USER_CACHE_TTL_SECONDS", "300"))
_CONV_CACHE_TTL = int(os.getenv("CONV_CACHE_TTL_SECONDS", "3600"))
_MSG_CACHE_TTL  = int(os.getenv("MSG_CACHE_TTL_SECONDS", "86400"))

_Q_INBOUND  = "queue:inbound_saves"
_Q_OUTBOUND = "queue:outbound_saves"
_Q_STATUS   = "queue:status_updates"
_Q_WABA     = "queue:waba_patches"

_owner_cache = None


# ── Serialization helpers ─────────────────────────────────────────────────────

def _serialize_user(user) -> str:
    return json.dumps({
        "id": str(user.id),
        "phone": user.phone,
        "display_name": user.display_name,
        "is_owner": user.is_owner,
        "is_active": user.is_active,
        "deleted_at": user.deleted_at.isoformat() if user.deleted_at else None,
    })


def _serialize_conversation(conv) -> str:
    return json.dumps({
        "id": str(conv.id),
        "title": conv.title,
        "waba_chat_id": conv.waba_chat_id,
    })


# ── Lightweight cached objects ────────────────────────────────────────────────

class _CachedUser:
    def __init__(self, data: dict):
        self.id = uuid.UUID(data["id"])
        self.phone = data["phone"]
        self.display_name = data["display_name"]
        self.is_owner = data["is_owner"]
        self.is_active = data["is_active"]
        self.deleted_at = data["deleted_at"]


class _CachedConversation:
    def __init__(self, data: dict):
        self.id = uuid.UUID(data["id"])
        self.title = data["title"]
        self.waba_chat_id = data["waba_chat_id"]


class _CachedMessage:
    def __init__(self, data: dict):
        self.id = uuid.UUID(data["id"])
        self.waba_message_id = data.get("waba_message_id")
        self.status = data.get("status", "pending")


# ── Sender resolution ─────────────────────────────────────────────────────────

async def resolve_sender(from_number: str):
    """
    Returns (user, conversation) or (None, None) if unauthorized.
    Redis-first when USE_REDIS_CACHE=true.
    """
    if USE_REDIS_CACHE:
        return await _resolve_sender_cached(from_number)
    return await _resolve_sender_db(from_number)


async def _resolve_sender_db(from_number: str):
    with new_span("resolve_all"):
        user, conversation = await asyncio.gather(
            DBService.get_user_by_phone(from_number),
            DBService.get_or_create_conversation(from_number, user.id),
        )
        
        if not user:
            return None, None, "not_found"
        if not user.is_active:
            return None, None, "inactive"
        if user.deleted_at is not None:
            return None, None, "deleted"

        if not conversation:
            log.error("Default 'PA' conversation not found — was the DB seeded?")
            return None, None

        log.debug("Auth ok for %s | conversation=%s", from_number, conversation.id)

        if not conversation.waba_chat_id:
            log.debug("First message — linking waba_chat_id for conversation %s", conversation.id)
            await DBService.set_conversation_waba_id(conversation.id, from_number)

        return user, conversation


async def _resolve_sender_cached(from_number: str):
    with new_span("resolve_all.cached"):
        user_key = f"user:{from_number}"
        conv_key = "conversation:default"

        raw_user, raw_conv = await asyncio.gather(
            RedisService.cache_get(user_key),
            RedisService.cache_get(conv_key),
        )

        # ── User ──
        if raw_user:
            log.debug("Cache hit: user %s", from_number)
            user = _CachedUser(json.loads(raw_user))
        else:
            log.debug("Cache miss: user %s — fetching from DB", from_number)
            user = await DBService.get_user_by_phone(from_number)
            if user:
                await RedisService.cache_set(user_key, _serialize_user(user), ttl=_USER_CACHE_TTL)

        if not user:
            return None, None, "not_found"
        if not user.is_active:
            return None, None, "inactive"
        if user.deleted_at is not None:
            return None, None, "deleted"

        # ── Conversation ──
        if raw_conv:
            log.debug("Cache hit: conversation:default")
            conversation = _CachedConversation(json.loads(raw_conv))
        else:
            log.debug("Cache miss: conversation:default — fetching from DB", )
            conversation = await DBService.get_or_create_conversation(from_number, user.id)
            if not conversation:
                log.error("Default 'PA' conversation not found — was the DB seeded?")
                return None, None
            await RedisService.cache_set(conv_key, _serialize_conversation(conversation), ttl=_CONV_CACHE_TTL)
            if not conversation.waba_chat_id:
                log.debug("First message — linking waba_chat_id for conversation %s", conversation.id)
                await DBService.set_conversation_waba_id(conversation.id, from_number)

        log.debug("Auth ok for %s | conversation=%s", from_number, conversation.id)
        return user, conversation


# ── History ───────────────────────────────────────────────────────────────────

async def load_history(from_number: str) -> list[dict]:
    """Load conversation history from Redis."""
    with new_span("redis.load_history"):
        history = await RedisService.get_history(from_number)
        log.debug("Loaded %d history entries for %s", len(history), from_number)
        return history


async def save_history(from_number: str, history: list[dict], body: str, reply_text: str) -> None:
    """Append user + assistant turn to Redis and refresh TTL."""
    with new_span("redis.save_history"):
        updated = history + [
            {"role": "user", "content": body},
            {"role": "assistant", "content": reply_text},
        ]
        await RedisService.save_history(from_number, updated)
        await RedisService.refresh_history_ttl(from_number)
        log.debug("History saved for %s (%d entries)", from_number, len(updated))


# ── Inbound save ──────────────────────────────────────────────────────────────

def dispatch_inbound_save(
    conversation_id, msg_type, body, sender_id,
    sender_type, waba_message_id, reply_to_waba_id, raw_metadata,
) -> None:
    """Save inbound message — queued to Redis or direct DB depending on mode."""
    if USE_REDIS_CACHE:
        asyncio.create_task(_queue_inbound_save(
            conversation_id=str(conversation_id),
            msg_type=msg_type,
            body=body,
            sender_id=str(sender_id) if sender_id else None,
            sender_type=sender_type,
            waba_message_id=waba_message_id,
            reply_to_waba_id=reply_to_waba_id,
            raw_metadata=raw_metadata,
        ))
        log.debug("Inbound save queued to Redis")
    else:
        asyncio.create_task(
            DBService.save_message(
                conversation_id=conversation_id,
                direction="inbound",
                msg_type=msg_type,
                body=body,
                sender_id=sender_id,
                waba_message_id=waba_message_id,
                reply_to_waba_id=reply_to_waba_id,
                metadata=raw_metadata,
                is_llm_generated=False,
                sender_type=sender_type,
            )
        )
        log.debug("Inbound DB write dispatched to background")


async def _queue_inbound_save(**kwargs) -> None:
    await _client.rpush(_Q_INBOUND, json.dumps({
        **kwargs,
        "direction": "inbound",
        "is_llm_generated": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }))


# ── Outbound save ─────────────────────────────────────────────────────────────

async def save_outbound_message(conversation_id, reply_text, sender_id, waba_message_id):
    """
    Save outbound message.
    Redis mode: write instantly so status updates can find it.
    DB mode: write directly (must exist before Meta fires status webhook).
    """
    if USE_REDIS_CACHE:
        return await _save_outbound_redis(conversation_id, reply_text, sender_id, waba_message_id)
    return await _save_outbound_db(conversation_id, reply_text, sender_id, waba_message_id)


async def _save_outbound_redis(conversation_id, reply_text, sender_id, waba_message_id):
    with new_span("redis.save_outbound"):
        temp_id = str(uuid.uuid4())
        msg_data = {
            "id": temp_id,
            "conversation_id": str(conversation_id),
            "direction": "outbound",
            "msg_type": "text",
            "body": reply_text,
            "sender_id": str(sender_id) if sender_id else None,
            "sender_type": "llm",
            "is_llm_generated": True,
            "waba_message_id": None,
            "reply_to_waba_id": waba_message_id,
            "status": "pending",
            "metadata": {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await _client.set(f"msg:{temp_id}", json.dumps(msg_data), ex=_MSG_CACHE_TTL)
        await _client.rpush(_Q_OUTBOUND, json.dumps(msg_data))
        log.debug("Outbound message queued to Redis id=%s", temp_id)
        return _CachedMessage(msg_data)


async def _save_outbound_db(conversation_id, reply_text, sender_id, waba_message_id):
    with new_span("db.save_outbound"):
        outbound_msg = await DBService.save_message(
            conversation_id=conversation_id,
            direction="outbound",
            msg_type="text",
            body=reply_text,
            sender_id=sender_id,
            waba_message_id=None,
            reply_to_waba_id=waba_message_id,
            metadata={},
            is_llm_generated=True,
            sender_type="llm",
        )
        log.debug("Outbound message saved id=%s", outbound_msg.id if outbound_msg else None)
        return outbound_msg


# ── Waba ID patch ─────────────────────────────────────────────────────────────

def dispatch_waba_id_patch(outbound_msg, waba_reply_id: str) -> None:
    """Patch waba_message_id onto the outbound message after send."""
    if not outbound_msg or not waba_reply_id:
        return
    if USE_REDIS_CACHE:
        asyncio.create_task(_patch_waba_id_redis(str(outbound_msg.id), waba_reply_id))
    else:
        asyncio.create_task(DBService.update_message_waba_id(outbound_msg.id, waba_reply_id))
    log.debug("Waba ID patch dispatched for msg=%s wamid=%s", outbound_msg.id, waba_reply_id)


async def _patch_waba_id_redis(msg_id: str, waba_reply_id: str) -> None:
    async with _client.pipeline(transaction=True) as pipe:
        await pipe.get(f"msg:{msg_id}")
        results = await pipe.execute()
        raw = results[0]
        if raw:
            msg_data = json.loads(raw)
            msg_data["waba_message_id"] = waba_reply_id
            await pipe.set(f"msg:{msg_id}", json.dumps(msg_data), ex=_MSG_CACHE_TTL)
            await pipe.set(f"msg:waba:{waba_reply_id}", msg_id, ex=_MSG_CACHE_TTL)
        await pipe.rpush(_Q_WABA, json.dumps({"msg_id": msg_id, "waba_message_id": waba_reply_id}))
        await pipe.execute()
    log.debug("Waba ID patched in Redis: msg=%s wamid=%s", msg_id, waba_reply_id)



# ── Status update ─────────────────────────────────────────────────────────────

async def handle_status_update_cached(waba_message_id: str, status: str) -> bool:
    """
    Update message status.
    Redis mode: update instantly in Redis, queue for DB flush.
    DB mode: update directly in DB.
    """
    if USE_REDIS_CACHE:
        return await _update_status_redis(waba_message_id, status)
    return await DBService.update_message_status(waba_message_id, status)


async def _update_status_redis(waba_message_id: str, status: str) -> bool:
    msg_id = await _client.get(f"msg:waba:{waba_message_id}")

    async with _client.pipeline(transaction=True) as pipe:
        if msg_id:
            await pipe.get(f"msg:{msg_id}")
            results = await pipe.execute()
            raw = results[0]
            if raw:
                msg_data = json.loads(raw)
                msg_data["status"] = status
                now = datetime.now(timezone.utc).isoformat()
                if status == "sent":
                    msg_data["sent_at"] = now
                elif status == "delivered":
                    msg_data["delivered_at"] = now
                elif status == "read":
                    msg_data["read_at"] = now
                await pipe.set(f"msg:{msg_id}", json.dumps(msg_data), ex=_MSG_CACHE_TTL)
        else:
            log.debug("Status update: Redis miss for waba_id=%s — queuing for DB", waba_message_id)

        await pipe.rpush(_Q_STATUS, json.dumps({
            "waba_message_id": waba_message_id,
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }))
        await pipe.execute()

    return msg_id is not None

# ── Owner cache ───────────────────────────────────────────────────────────────

async def get_owner_cached():
    """Fetch owner once and cache in-process for lifetime of the worker."""
    global _owner_cache
    if _owner_cache is None:
        _owner_cache = await DBService.get_owner()
        log.debug("Owner cached: id=%s", _owner_cache.id if _owner_cache else None)
    return _owner_cache


# ── WhatsApp send ─────────────────────────────────────────────────────────────

async def send_whatsapp_reply(wa, to_number: str, text: str) -> str | None:
    """Send a WhatsApp text message. Returns wamid or None on failure."""
    with new_span("wa.send"):
        try:
            message = wa.create_message(to=to_number, content=text)
            future = await message.send()
            response = await future
            wamid = None
            if isinstance(response, dict):
                messages = response.get("messages", [])
                if messages:
                    wamid = messages[0].get("id")
            log.success("WA message sent to %s wamid=%s", to_number, wamid)
            return wamid
        except Exception as e:
            log.exception("Failed to send WA message to %s: %s", to_number, e)
            return None