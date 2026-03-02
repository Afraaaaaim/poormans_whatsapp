"""
tasks.py — Celery tasks
========================
Location: once/tasks.py

Tasks:
    rotate_logs — nightly log archive
"""

from once.celery_app import celery_app
from once.logger import archive_logs, get_logger

log = get_logger(__name__)


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