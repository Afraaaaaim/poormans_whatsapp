"""
tasks.py — Celery tasks
=======================
Location: once/tasks.py

Add any background / scheduled tasks here.
Import celery_app from once.celery_app — never create a second Celery instance.
"""

from once.celery_app import celery_app
from once.logger import archive_logs, get_logger

log = get_logger(__name__)


@celery_app.task(
    name="once.tasks.rotate_logs",
    bind=True,
    max_retries=3,
    default_retry_delay=60,  # wait 60 s before retrying on failure
)
def rotate_logs(self):
    """
    Nightly log archive task.
    Triggered by Celery Beat at 00:01 IST via the schedule in celery_app.py.

    Retries up to 3 times (60 s apart) if something goes wrong —
    e.g. a file lock or disk hiccup at the exact moment of rotation.
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
