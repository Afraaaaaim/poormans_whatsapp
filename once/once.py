
"""
once.py — Orchestrator
======================
Thin coordinator only. All logic lives in helper_function.py and services.
"""
import os
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
ADMIN_DISPLAY_NAME = os.getenv("ADMIN_DISPLAY_NAME","Admin")

_REJECTION_MESSAGES = {
    "not_found": (
        "👋 *Hey there!*\n\n"
        "It looks like you don't have access to this assistant yet.\n\n"
        f"📩 Reach out to *{ADMIN_DISPLAY_NAME}* to get added!"
    ),
    "inactive": (
        "😴 *Your account is currently inactive.*\n\n"
        "Looks like your access has been paused for now.\n\n"
        "📩 Drop a message to the admin and they'll get you sorted!"
    ),
    "deleted": (
        "💔 *Your account has been removed.*\n\n"
        "It seems your access has been revoked.\n\n"
        "📩 If you think this is a mistake, contact the admin."
    ),
}

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
    user, conversation, reason = await resolve_sender(from_number,wa)
    if not user:
        await send_whatsapp_reply(wa, from_number, _REJECTION_MESSAGES.get(reason, "Access denied."))
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