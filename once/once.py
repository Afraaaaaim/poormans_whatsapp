"""
once.py — Orchestrator
======================
Thin coordinator only. All logic lives in helper_function.py and services.

LLM1 handoff signal format (emitted by Cerebras when tools are needed):
    ##AGENT::<reason>::<compressed_summary>##

If LLM1 emits this, the agent loop runs and its result is fed back to LLM1
for a final plain-text reply to the user.
"""
import os
import re
import asyncio
from once.llm_services import LLMService, SYSTEM_PROMPT
from once.logger import get_logger, new_span
from once.messages import REJECTION_MESSAGES
from once.helper_functions import (
    handle_status_update_cached,
    resolve_sender,
    load_history,
    save_history,
    dispatch_inbound_save,
    get_owner_cached,
    save_outbound_message,
    dispatch_waba_id_patch,
    send_whatsapp_reply,
)
from once.agent_service import agent_run

log = get_logger(__name__)

# LLM1 system prompt — base prompt + handoff instruction
SYSTEM_PROMPT_WITH_HANDOFF = SYSTEM_PROMPT + (
    "\n\nIf the user's request requires taking an action (adding/removing users, "
    "looking up data, making changes), do NOT answer directly. Instead emit ONLY "
    "this signal and nothing else:\n"
    "##AGENT::<one-line reason>::<1-2 sentence conversation summary>##\n"
    "Do not add any other text when emitting this signal."
)

# Matches: ##AGENT::<reason>::<compressed_summary>##
_HANDOFF_RE = re.compile(r"##AGENT::(.+?)::(.+?)##", re.DOTALL)


def _parse_handoff(reply: str) -> tuple[str, str] | None:
    """
    Returns (reason, compressed_summary) if reply is a handoff signal.
    Returns None if it's a normal conversational reply.
    """
    m = _HANDOFF_RE.search(reply.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None


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
    user, conversation, reason = await resolve_sender(from_number)
    if not user:
        await send_whatsapp_reply(wa, from_number, REJECTION_MESSAGES.get(reason, "Access denied."))
        return

    sender_type, sender_id = "human_owner" if user.is_owner else "human_user", user.id

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

    # ── 4. LOAD HISTORY + LLM1 ───────────────────────────────────────────────
    history = await load_history(from_number)
    messages_for_llm = history + [{"role": "user", "content": body}]

    try:
        llm1_reply = await LLMService.chat(
            messages=messages_for_llm,
            system_prompt=SYSTEM_PROMPT_WITH_HANDOFF,
        )
    except Exception:
        log.exception("LLM call failed for %s", from_number)
        await send_whatsapp_reply(wa, from_number, "⚠️ Something went wrong. Try again.")
        return

    # ── 5. HANDOFF CHECK ──────────────────────────────────────────────────────
    handoff = _parse_handoff(llm1_reply)

    if handoff:
        agent_reason, compressed_summary = handoff
        log.info("Handoff detected for %s | reason=%s", from_number, agent_reason)

        try:
            agent_result = await agent_run(
                wa=wa,
                reason=agent_reason,
                compressed_summary=compressed_summary,
                user_phone=from_number,
                caller_role=user.role,
            )
        except Exception:
            log.exception("Agent loop failed for %s", from_number)
            await send_whatsapp_reply(wa, from_number, "⚠️ Action failed. Try again.")
            return

        # Feed agent result back to LLM1 for a natural final reply
        try:
            final_messages = messages_for_llm + [
                {"role": "assistant", "content": llm1_reply},
                {
                    "role": "user",
                    "content": (
                        f"[AGENT RESULT]\n{agent_result}\n\n"
                        "Summarise what was done in a short, friendly WhatsApp reply."
                    ),
                },
            ]
            reply_text = await LLMService.chat(messages=final_messages)
        except Exception:
            log.exception("LLM1 final reply failed for %s", from_number)
            # Fall back to raw agent result rather than leaving user hanging
            reply_text = agent_result
    else:
        reply_text = llm1_reply

    # ── 6. SAVE HISTORY + FETCH OWNER (parallel) ─────────────────────────────
    _, owner = await asyncio.gather(
        save_history(from_number, history, body, reply_text),
        get_owner_cached(),
    )

    # ── 7. SAVE OUTBOUND + SEND ───────────────────────────────────────────────
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
        updated = await handle_status_update_cached(waba_message_id, status)
        if updated:
            log.success("Status updated: %s → %s", waba_message_id, status)
        else:
            log.warning("Status update: message not found waba_id=%s", waba_message_id)