"""
once.py — Central orchestrator
================================
Location: once/once.py

All services meet here. main.py hands off every inbound webhook and
once.py decides what to do.

Flow for an inbound text message:
    1. Auth check              — is this phone allowed?
    2. Resolve conversation    — find default PA conversation
    3. Load history            — from Redis via RedisService
    4. Save inbound msg to DB  — via DBService
    5. Build message list      — history + new user message
    6. LLM call                — via LLMService (pure I/O)
    7. Save reply to history   — back to Redis via RedisService
    8. Send reply via WA       — via the wa client
    9. Save outbound msg to DB — via DBService

Delivery status updates (from Meta webhooks):
    handle_status_update() — updates message status in DB
"""
import asyncio
from once.db_services import DBService
from once.llm_services import LLMService
from once.logger import get_logger, new_span
from once.redis_service import RedisService
from datetime import datetime
log = get_logger(__name__)

"""
once.py — Orchestrator
======================
Thin coordinator only. All logic lives in helper_function.py and services.
"""
import asyncio
from once.llm_services import LLMService
from once.logger import get_logger, new_span
from once.helper_functions import (
    resolve_sender,
    load_history,
    save_history,
    dispatch_inbound_save,
    get_owner_cached,
    save_outbound_message,
    dispatch_waba_id_patch,
    send_whatsapp_reply,
)

log = get_logger(__name__)


async def handle_inbound_message(
    wa,
    from_number: str,
    waba_message_id: str | None,
    msg_type: str,
    body: str | None,
    reply_to_waba_id: str | None = None,
    raw_metadata: dict | None = None,
) -> None:

    # ── 1. AUTH + RESOLVE ─────────────────────────────────────────────────────
    user, conversation = await resolve_sender(from_number)
    if not user or not conversation:
        return

    sender_type,sender_id = "human_owner" if user.is_owner else "human_user" , user.id

    # ── 2. SAVE INBOUND (background) ──────────────────────────────────────────
    dispatch_inbound_save(
        conversation_id=conversation.id,
        msg_type=msg_type,
        body=body,
        sender_id=sender_id,
        sender_type=sender_type,
        waba_message_id=waba_message_id,
        reply_to_waba_id=reply_to_waba_id,
        raw_metadata=raw_metadata or {},
    )

    # ── 3. TEXT ONLY ──────────────────────────────────────────────────────────
    if msg_type != "text" or not body:
        log.info("Non-text message (type=%s) — skipping LLM", msg_type)
        return

    # ── 4. LOAD HISTORY + LLM ─────────────────────────────────────────────────
    history = await load_history(from_number)
    messages_for_llm = history + [{"role": "user", "content": body}]

    try:
        reply_text = await LLMService.chat(messages=messages_for_llm)
    except Exception:
        log.exception("LLM call failed for %s", from_number)
        await send_whatsapp_reply(wa, from_number, "⚠️ Something went wrong. Try again.")
        return

    # ── 5. SAVE HISTORY + FETCH OWNER (parallel) ─────────────────────────────
    _, owner = await asyncio.gather(
        save_history(from_number, history, body, reply_text),
        get_owner_cached(),
    )

    # ── 6. SAVE OUTBOUND + SEND ───────────────────────────────────────────────
    outbound_msg = await save_outbound_message(
        conversation_id=conversation.id,
        reply_text=reply_text,
        sender_id=owner.id if owner else None,
        waba_message_id=waba_message_id,
    )

    waba_reply_id = await send_whatsapp_reply(wa, from_number, reply_text)
    dispatch_waba_id_patch(outbound_msg, waba_reply_id)

    log.success("Reply sent to %s", from_number)


async def handle_status_update(waba_message_id: str, status: str) -> None:
    """Update message delivery status in DB."""
    with new_span("db.status_update"):
        log.debug("Status update: %s → %s", waba_message_id, status)
        from once.db_services import DBService
        updated = await DBService.update_message_status(waba_message_id, status)
        if updated:
            log.success("Status updated: %s → %s", waba_message_id, status)
        else:
            log.warning("Status update: message not found waba_id=%s", waba_message_id)