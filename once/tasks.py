"""
tasks.py — Celery tasks
========================
Location: once/tasks.py

Tasks:
    rotate_logs        — nightly log archive (existing)
    flush_llm_history  — periodic Redis → DB history flush

Note on async: Celery workers are sync. All async work is done inside
RedisService.sync_* wrappers and DBService is called via asyncio.run()
in a single contained helper. tasks.py itself never imports asyncio directly.
"""

import asyncio

from once.celery_app import celery_app
from once.logger import archive_logs, get_logger
from once.redis_service import RedisService

log = get_logger(__name__)


# ── Nightly log rotation ──────────────────────────────────────────────────────


@celery_app.task(
    name="once.tasks.rotate_logs",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def rotate_logs(self):
    """
    Nightly log archive. Triggered by Celery Beat at 00:01 IST.
    Retries up to 3 times (60s apart) on failure.
    """
    log.info("rotate_logs: starting nightly log archive")
    try:
        archive_logs()
        log.success("rotate_logs: archive complete")
    except Exception as exc:
        log.exception(
            "rotate_logs: failed — retrying (attempt %d/3)", self.request.retries + 1
        )
        raise self.retry(exc=exc)


# ── Redis → DB history flush ──────────────────────────────────────────────────


@celery_app.task(
    name="once.tasks.flush_llm_history",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def flush_llm_history(self):
    """
    Periodically reads all active LLM conversation histories from Redis
    and writes any un-persisted messages to the DB.

    Why: Redis is fast + ephemeral. DB is on a separate server with
    plenty of space. This bridges the two without write-through overhead.

    Schedule: every HISTORY_FLUSH_EVERY minutes (set in celery_app.py).
    """
    log.info("flush_llm_history: starting Redis → DB flush")
    try:
        # Use sync wrapper — no asyncio.run() needed here
        phones = RedisService.sync_get_all_history_phones()

        if not phones:
            log.debug("flush_llm_history: no active histories — nothing to flush")
            return

        log.debug("flush_llm_history: flushing %d active conversations", len(phones))
        count = asyncio.run(_flush_histories(phones))
        log.success(
            "flush_llm_history: flushed %d entries across %d conversations",
            count,
            len(phones),
        )

    except Exception as exc:
        log.exception("flush_llm_history: failed — retrying")
        raise self.retry(exc=exc)


async def _flush_histories(phones: list[str]) -> int:
    """
    Async implementation — called via asyncio.run() from flush_llm_history.
    Reads history for each phone and writes un-persisted entries to DB.
    Returns total rows saved.
    """
    from once.db_services import DBService

    conversation = await DBService.get_default_conversation()
    if not conversation:
        log.error("flush_llm_history: default conversation not found — aborting")
        return 0

    owner = await DBService.get_owner()
    total = 0

    for phone in phones:
        history = await RedisService.get_history(phone)
        user = await DBService.get_user_by_phone(phone)

        # Only flush entries that have no waba_message_id — these are the ones
        # not yet individually persisted by once.py (acts as a safety net).
        # Entries saved by once.py already have a wamid and are already in DB.
        rows = []
        for entry in history:
            role = entry.get("role")
            content = entry.get("content", "")

            # Skip entries that were already individually persisted
            if entry.get("waba_message_id"):
                continue

            if role == "user":
                rows.append(
                    {
                        "conversation_id": conversation.id,
                        "sender_id": user.id if user else None,
                        "direction": "inbound",
                        "msg_type": "text",
                        "body": content,
                        "status": "delivered",
                        "is_llm_generated": False,
                        "sender_type": (
                            "human_owner" if user and user.is_owner else "human_user"
                        ),
                        "metadata": {"source": "redis_flush"},
                    }
                )
            elif role == "assistant":
                rows.append(
                    {
                        "conversation_id": conversation.id,
                        "sender_id": owner.id if owner else None,
                        "direction": "outbound",
                        "msg_type": "text",
                        "body": content,
                        "status": "sent",
                        "is_llm_generated": True,
                        "sender_type": "llm",
                        "metadata": {"source": "redis_flush"},
                    }
                )

        if rows:
            saved = await DBService.bulk_save_messages(rows)
            log.debug("flush_llm_history: saved %d rows for %s", saved, phone)
            total += saved

    return total
