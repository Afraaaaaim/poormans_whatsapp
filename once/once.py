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
    """
    Entry point for every inbound WhatsApp message.
    Called by main.py's @wa.on_message handler.

    wa               — AsyncWhatsApp client (needed to send replies)
    from_number      — E.164 phone of the sender
    waba_message_id  — Meta's wamid.xxx for this message
    msg_type         — 'text', 'image', 'audio', etc.
    body             — text content (None for media-only messages)
    reply_to_waba_id — wamid of quoted message if replying
    raw_metadata     — full raw webhook payload for audit
    """

    # ── 1. AUTH ───────────────────────────────────────────────────────────────
    with new_span("auth"):
        authorized = await DBService.is_authorized(from_number)
        if not authorized:
            log.warning("Unauthorized message from %s — ignoring", from_number)
            return
        log.debug("Authorized: %s", from_number)

    # ── 2. RESOLVE CONVERSATION ───────────────────────────────────────────────
    with new_span("resolve_conversation"):
        conversation = await DBService.get_default_conversation()
        if not conversation:
            log.error("Default 'PA' conversation not found — was the DB seeded?")
            return

        # Link Meta's sender phone as waba_chat_id on first message
        if not conversation.waba_chat_id:
            await DBService.set_conversation_waba_id(conversation.id, from_number)

    # ── 3. RESOLVE SENDER ─────────────────────────────────────────────────────
    with new_span("resolve_user"):
        user = await DBService.get_user_by_phone(from_number)
        sender_id = user.id if user else None
        sender_type = "human_owner" if user and user.is_owner else "human_user"

    # ── 4. SAVE INBOUND MESSAGE TO DB ─────────────────────────────────────────
    asyncio.create_task(
        DBService.save_message(
            conversation_id=conversation.id,
            direction="inbound",
            msg_type=msg_type,
            body=body,
            sender_id=sender_id,
            waba_message_id=waba_message_id,
            reply_to_waba_id=reply_to_waba_id,
            metadata=raw_metadata or {},
            is_llm_generated=False,
            sender_type=sender_type,
        )
    )
    log.debug("Inbound DB write dispatched to background for %s", from_number)

    # ── 5. ONLY RESPOND TO TEXT MESSAGES ─────────────────────────────────────
    if msg_type != "text" or not body:
        log.info("Non-text message (type=%s) — skipping LLM", msg_type)
        return

    # ── 6. LOAD HISTORY + BUILD MESSAGE LIST ──────────────────────────────────
    with new_span("redis.load_history"):
        history = await RedisService.get_history(from_number)
        log.debug("Loaded %d history entries for %s", len(history), from_number)

    # Append the new user message to build the full context for the LLM
    messages_for_llm = history + [{"role": "user", "content": body}]

    # ── 7. LLM CALL ───────────────────────────────────────────────────────────
    try:
        reply_text = await LLMService.chat(messages=messages_for_llm)
    except Exception:
        log.exception("LLM call failed for %s", from_number)
        await _send_whatsapp_reply(
            wa, from_number, "⚠️ Something went wrong. Try again."
        )
        return

    # ── 8. SAVE UPDATED HISTORY TO REDIS ─────────────────────────────────────
    with new_span("redis.save_history"):
        updated_history = history + [
            {"role": "user", "content": body},
            {"role": "assistant", "content": reply_text},
        ]
        await RedisService.save_history(from_number, updated_history)
        # Refresh TTL so active users never expire mid-conversation
        await RedisService.refresh_history_ttl(from_number)

        
        # ── 9. RESOLVE OWNER (needed for outbound DB row) ─────────────────────────
        owner = await DBService.get_owner()

        # ── 10. SEND REPLY VIA WHATSAPP ───────────────────────────────────────────
        # Outbound DB write MUST happen before send — Meta fires "sent" status
        # webhook almost instantly, and the row must exist for it to land on.
        with new_span("db.save_outbound"):
            # We don't have waba_reply_id yet — save with None first, update after send
            outbound_msg = await DBService.save_message(
                conversation_id=conversation.id,
                direction="outbound",
                msg_type="text",
                body=reply_text,
                sender_id=owner.id if owner else None,
                waba_message_id=None,          # not known yet
                reply_to_waba_id=waba_message_id,
                metadata={},
                is_llm_generated=True,
                sender_type="llm",
            )

        with new_span("wa.send"):
            waba_reply_id = await _send_whatsapp_reply(wa, from_number, reply_text)

        # ── 11. PATCH waba_message_id ONTO THE OUTBOUND ROW ──────────────────────
        # Now that Meta gave us the wamid, stamp it so status webhooks can find the row.
        if waba_reply_id and outbound_msg:
            asyncio.create_task(
                DBService.update_message_waba_id(outbound_msg.id, waba_reply_id)
            )
            log.debug("Outbound waba_id patch dispatched to background")

        log.success("Reply sent to %s", from_number)



async def handle_status_update(waba_message_id: str, status: str) -> None:
    """
    Called when Meta sends a delivery receipt webhook.
    Updates message status (sent → delivered → read) in DB.
    """
    with new_span("db.status_update"):
        log.debug("Status update: %s → %s", waba_message_id, status)
        updated = await DBService.update_message_status(waba_message_id, status)
        if updated:
            log.success("Status updated: %s → %s", waba_message_id, status)
        else:
            log.warning("Status update: message not found waba_id=%s", waba_message_id)


# ── Internal helpers ──────────────────────────────────────────────────────────


async def _send_whatsapp_reply(wa, to_number: str, text: str) -> str | None:
    """Send a WhatsApp text message. Returns the wamid or None on failure."""
    try:
        message = wa.create_message(to=to_number, content=text)
        future = await message.send()
        response = await future  # send() returns a Future, await it for the actual JSON

        # Meta API response format: {"messages": [{"id": "wamid.xxx"}]}
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
