"""
celery_app.py — Celery application + Beat schedule
===================================================
Location: once/celery_app.py

ENV VARS:
    REDIS_URL       — broker URL            (default: redis://redis:6379/0)
    LOG_ROTATE_TZ   — timezone for Beat     (default: Asia/Kolkata)
    LOG_ROTATE_HOUR — hour  to run archive  (default: 0  → midnight)
    LOG_ROTATE_MIN  — minute to run archive (default: 1  → 00:01)

Example .env:
    REDIS_URL=redis://redis:6379/0
    LOG_ROTATE_TZ=Asia/Kolkata
    LOG_ROTATE_HOUR=0
    LOG_ROTATE_MIN=1
"""

import os

from celery import Celery
from celery.schedules import crontab

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
LOG_ROTATE_TZ = os.getenv("LOG_ROTATE_TZ", "Asia/Kolkata")
LOG_ROTATE_HOUR = int(os.getenv("LOG_ROTATE_HOUR", "0"))
LOG_ROTATE_MIN = int(os.getenv("LOG_ROTATE_MIN", "1"))

celery_app = Celery(
    "poormans_whatsapp",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["once.tasks"],
)

celery_app.conf.update(
    # ── serialisation ──
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # ── timezone ──
    timezone=LOG_ROTATE_TZ,
    enable_utc=True,
    # ── reliability ──
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # ── Beat schedule ──
    beat_schedule={
        "nightly-log-archive": {
            "task": "once.tasks.rotate_logs",
            "schedule": crontab(hour=LOG_ROTATE_HOUR, minute=LOG_ROTATE_MIN),
            "options": {"expires": 3600},
        },
    },
)