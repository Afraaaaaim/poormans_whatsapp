"""
tasks.py — Celery tasks
"""
import json
from once.celery_app import celery_app
from once.logger import archive_logs, get_logger

log = get_logger(__name__)

_Q_INBOUND  = "queue:inbound_saves"
_Q_OUTBOUND = "queue:outbound_saves"
_Q_STATUS   = "queue:status_updates"
_Q_WABA     = "queue:waba_patches"


@celery_app.task(name="once.tasks.rotate_logs", bind=True, max_retries=3, default_retry_delay=60)
def rotate_logs(self):
    log.info("rotate_logs: starting nightly log archive")
    try:
        archive_logs()
        log.success("rotate_logs: archive complete")
    except Exception as exc:
        log.exception("rotate_logs: failed — retrying (attempt %d/3)", self.request.retries + 1)
        raise self.retry(exc=exc)


@celery_app.task(name="once.tasks.flush_db_queue", bind=True, max_retries=3, default_retry_delay=30)
def flush_db_queue(self):
    """Drain Redis write-behind queues into DB. Runs every 30s via Beat."""
    import asyncio
    try:
        asyncio.run(_flush_all())
    except Exception as exc:
        log.exception("flush_db_queue: failed — retrying")
        raise self.retry(exc=exc)


async def _flush_all():
    import os
    import redis.asyncio as aioredis
    from once.db_services import DBService
    client = aioredis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
    import asyncio
    await asyncio.gather(
        _flush_inbound(client, DBService),
        _flush_outbound(client, DBService),
        _flush_status(client, DBService),
        _flush_waba_patches(client, DBService),
    )
    await client.aclose()


async def _drain_queue(client, key):
    items = []
    while True:
        raw = await client.lpop(key)
        if raw is None:
            break
        try:
            items.append(json.loads(raw))
        except json.JSONDecodeError:
            log.warning("flush_db_queue: corrupt item in %s — skipping", key)
    return items


async def _flush_inbound(client, DBService):
    items = await _drain_queue(client, _Q_INBOUND)
    if not items:
        return
    log.info("flush_db_queue: flushing %d inbound saves", len(items))
    for item in items:
        try:
            await DBService.save_message(
                conversation_id=item["conversation_id"], direction="inbound",
                msg_type=item["msg_type"], body=item.get("body"),
                sender_id=item.get("sender_id"), waba_message_id=item.get("waba_message_id"),
                reply_to_waba_id=item.get("reply_to_waba_id"), metadata=item.get("raw_metadata", {}),
                is_llm_generated=False, sender_type=item.get("sender_type"),
            )
        except Exception:
            log.exception("flush_db_queue: failed inbound — requeueing")
            await client.rpush(_Q_INBOUND, json.dumps(item))
    log.success("flush_db_queue: %d inbound saves flushed", len(items))


async def _flush_outbound(client, DBService):
    items = await _drain_queue(client, _Q_OUTBOUND)
    if not items:
        return
    log.info("flush_db_queue: flushing %d outbound saves", len(items))
    for item in items:
        try:
            await DBService.save_message(
                conversation_id=item["conversation_id"], direction="outbound",
                msg_type=item.get("msg_type", "text"), body=item.get("body"),
                sender_id=item.get("sender_id"), waba_message_id=item.get("waba_message_id"),
                reply_to_waba_id=item.get("reply_to_waba_id"), metadata=item.get("metadata", {}),
                is_llm_generated=True, sender_type="llm",
            )
        except Exception:
            log.exception("flush_db_queue: failed outbound — requeueing")
            await client.rpush(_Q_OUTBOUND, json.dumps(item))
    log.success("flush_db_queue: %d outbound saves flushed", len(items))


async def _flush_status(client, DBService):
    items = await _drain_queue(client, _Q_STATUS)
    if not items:
        return
    log.info("flush_db_queue: flushing %d status updates", len(items))
    for item in items:
        try:
            await DBService.update_message_status(item["waba_message_id"], item["status"])
        except Exception:
            log.exception("flush_db_queue: failed status — requeueing")
            await client.rpush(_Q_STATUS, json.dumps(item))
    log.success("flush_db_queue: %d status updates flushed", len(items))


async def _flush_waba_patches(client, DBService):
    items = await _drain_queue(client, _Q_WABA)
    if not items:
        return
    log.info("flush_db_queue: flushing %d waba patches", len(items))
    for item in items:
        try:
            await DBService.update_message_waba_id(item["msg_id"], item["waba_message_id"])
        except Exception:
            log.exception("flush_db_queue: failed waba patch — requeueing")
            await client.rpush(_Q_WABA, json.dumps(item))
    log.success("flush_db_queue: %d waba patches flushed", len(items))