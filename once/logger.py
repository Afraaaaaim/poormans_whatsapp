"""
logger.py — Centralized logger

Usage:
    from logger import get_logger, set_request_context, new_span

    log = get_logger(__name__)
    log.info("Hello world")
=============================================================================
    # At the start of handling a new request/message:
    set_request_context()           # auto-generates trace_id + request_id
    log.info("New request received")
=============================================================================
    # For a sub-operation (span):
    with new_span("call_llm"):
        log.debug("Sending prompt to LLM")
        # ... do work ...
        log.info("LLM responded")

        from logger import get_logger

==============================================================================

    # log = get_logger(__name__)   # __name__ auto-fills the module name

    # log.trace("Very verbose detail")
    # log.debug("Checking variable x=%s", x)
    # log.info("Server started on port 8080")
    # log.success("Message sent successfully")
    # log.warning("Rate limit approaching")
    # log.error("Failed to connect to DB")
    # log.critical("App is going down!")
    # log.exception("Caught exception")  # like error() but auto-attaches traceback

==============================================================================
from logger import get_logger, set_request_context, new_span

log = get_logger(__name__)

# At the TOP of handling each incoming WhatsApp message:
set_request_context()   # generates trace_id + request_id automatically
log.info("New message received")   # every log from here carries those IDs

# When calling a sub-system (span groups logs for one operation):
with new_span("llm_call"):
    log.debug("Sending to LLM")
    response = llm.generate(prompt)
    log.success("LLM responded")

with new_span("db_write"):
    log.debug("Saving to DB")

ENV VARS (in .env):
    LOG_LEVEL   — console verbosity: TRACE, DEBUG, INFO, WARNING, ERROR  (default: DEBUG)
    LOG_JSON    — "true" → write JSON files for Grafana/Kibana  (default: false)
    REDIS_URL   — broker for Celery  (default: redis://redis:6379/0)

LOG FILE LAYOUT:
    logs/
    ├── app.log / app.json         ← active, all levels
    ├── errors.log / errors.json   ← active, WARNING+ only
    └── archive/
        └── 2026-02-28/
            ├── app_2026-02-28.log.gz
            └── errors_2026-02-28.log.gz

ROTATION:
    archive_logs() is called by Celery Beat at 00:01 IST every night.
    It is a plain function — no scheduler lives inside this file.
    See once/celery_app.py and once/tasks.py.
"""

import gzip
import json
import logging
import os
import shutil
import sys
import traceback
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from opentelemetry import trace as _otel_trace

# ──────────────────────────────────────────────
# Context variables  (per-coroutine / per-thread)
# ──────────────────────────────────────────────
_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")
_span_id: ContextVar[str] = ContextVar("span_id", default="-")
_request_id: ContextVar[str] = ContextVar("request_id", default="-")
_span_name: ContextVar[str] = ContextVar("span_name", default="-")


def set_request_context(
    request_id: str | None = None,
    trace_id: str | None = None,
) -> dict:
    rid = request_id or str(uuid.uuid4())

    # Use active OTEL span if one exists
    span = _otel_trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.is_valid:
        tid = trace_id or format(ctx.trace_id, "032x")
        sid = format(ctx.span_id, "016x")
    else:
        tid = trace_id or str(uuid.uuid4())
        sid = "-"

    _request_id.set(rid)
    _trace_id.set(tid)
    _span_id.set(sid)
    _span_name.set("-")
    return {"request_id": rid, "trace_id": tid}


def clear_request_context() -> None:
    """Reset context after a request is done."""
    _trace_id.set("-")
    _span_id.set("-")
    _request_id.set("-")
    _span_name.set("-")


@contextmanager
def new_span(name: str):
    tracer = _otel_trace.get_tracer("once.logger")
    prev_span = _span_id.get()
    prev_name = _span_name.get()

    with tracer.start_as_current_span(name) as otel_span:
        ctx = otel_span.get_span_context()
        if ctx and ctx.is_valid:
            new_sid = format(ctx.span_id, "016x")
            new_tid = format(ctx.trace_id, "032x")
            _trace_id.set(new_tid)
        else:
            new_sid = str(uuid.uuid4())[:8]

        _span_id.set(new_sid)
        _span_name.set(name)
        try:
            yield new_sid
        finally:
            _span_id.set(prev_span)
            _span_name.set(prev_name)


# ──────────────────────────────────────────────
# Custom log levels
# ──────────────────────────────────────────────
TRACE_LEVEL = 5  # below DEBUG — very noisy step-by-step tracing
SUCCESS_LEVEL = 25  # between INFO(20) and WARNING(30)

logging.addLevelName(TRACE_LEVEL, "TRACE")
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")


# ──────────────────────────────────────────────
# Formatter  (TEXT console | JSON files)
# ──────────────────────────────────────────────
class RichFormatter(logging.Formatter):
    LEVEL_COLORS = {
        "TRACE": "\033[90m",  # dark grey
        "DEBUG": "\033[36m",  # cyan
        "INFO": "\033[32m",  # green
        "SUCCESS": "\033[92m",  # bright green
        "WARNING": "\033[33m",  # yellow
        "ERROR": "\033[31m",  # red
        "CRITICAL": "\033[41;97m",  # white on red background
    }
    RESET = "\033[0m"

    _SKIP_KEYS = frozenset(
        logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    )

    def __init__(self, use_json: bool = False, use_color: bool = True):
        super().__init__()
        self.use_json = use_json
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

        filepath = Path(record.pathname)
        try:
            rel_path = filepath.relative_to(Path.cwd())
        except ValueError:
            rel_path = filepath
        location = f"{rel_path}:{record.lineno} in {record.funcName}()"

        trace_id = _trace_id.get()
        span_id = _span_id.get()
        request_id = _request_id.get()
        span_name = _span_name.get()

        exc_text = ""
        if record.exc_info:
            exc_text = "".join(traceback.format_exception(*record.exc_info)).strip()

        if self.use_json:
            payload: dict = {
                "timestamp": ts,
                "level": record.levelname,
                "logger": record.name,
                "location": str(location),
                "message": record.getMessage(),
                "trace_id": trace_id,
                "span_id": span_id,
                "span_name": span_name,
                "request_id": request_id,
            }
            if exc_text:
                payload["exception"] = exc_text
            for key, val in record.__dict__.items():
                if key not in self._SKIP_KEYS and not key.startswith("_"):
                    payload[key] = val
            return json.dumps(payload, default=str)

        # ── TEXT mode (console always) ──
        level = f"{record.levelname:<8}"
        color = self.LEVEL_COLORS.get(record.levelname, "") if self.use_color else ""
        reset = self.RESET if self.use_color else ""

        ctx = f"trace={trace_id[:8]} span={span_id} req={request_id[:8]}"
        if span_name != "-":
            ctx += f" [{span_name}]"

        line = (
            f"{ts} | {color}{level}{reset} | "
            f"{record.name} | {location} | "
            f"{ctx} | "
            f"{record.getMessage()}"
        )
        if exc_text:
            line += f"\n{color}{exc_text}{reset}"
        return line


# ──────────────────────────────────────────────
# Archive logic  (called by Celery task — no scheduler here)
# ──────────────────────────────────────────────
_LOG_DIR = Path("logs")
_ARCHIVE_DIR = _LOG_DIR / "archive"

_internal_log = logging.getLogger("once.logger.archiver")


def archive_logs() -> None:
    """
    Compresses yesterday's log files into logs/archive/YYYY-MM-DD/ and
    truncates the originals in-place so running handlers keep writing
    without a restart.

    Skips any file that is empty — no empty zips are ever created.

    Called by the Celery task in once/tasks.py at 00:01 IST nightly.
    Can also be called manually for testing:
        from once.logger import archive_logs
        archive_logs()
    """
    # Timezone driven by LOG_ROTATE_TZ env var — same setting used by Celery Beat.
    tz_name = os.getenv("LOG_ROTATE_TZ", "Asia/Kolkata")
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+, present in the Docker image

        tz = ZoneInfo(tz_name)
    except Exception:
        _internal_log.warning(
            "archive_logs: unknown timezone %r, falling back to UTC", tz_name
        )
        tz = timezone.utc
    yesterday = (datetime.now(tz) - timedelta(days=1)).strftime("%Y-%m-%d")

    dest_dir = _ARCHIVE_DIR / yesterday
    use_json = os.getenv("LOG_JSON", "false").lower() == "true"
    ext = "json" if use_json else "log"

    candidates = [
        _LOG_DIR / f"app.{ext}",
        _LOG_DIR / f"errors.{ext}",
    ]

    archived_any = False

    for src in candidates:
        if not src.exists():
            continue
        if src.stat().st_size == 0:
            _internal_log.debug("archive_logs: skipping %s — empty file", src.name)
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        archived = dest_dir / f"{src.stem}_{yesterday}.{ext}.gz"

        try:
            with src.open("rb") as f_in, gzip.open(archived, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)

            # truncate in-place — file descriptor remains valid, no handler restart needed
            with src.open("w", encoding="utf-8"):
                pass

            _internal_log.info("archive_logs: %s → %s", src.name, archived)
            archived_any = True

        except Exception:
            _internal_log.exception("archive_logs: failed to archive %s", src.name)

    if not archived_any:
        _internal_log.info(
            "archive_logs: nothing to archive for %s (all files empty)", yesterday
        )


# ──────────────────────────────────────────────
# Logger setup  (runs once on first get_logger call)
# ──────────────────────────────────────────────
_SETUP_DONE = False


class _ExcludeSitePackagesFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return ".venv" not in record.pathname and "site-packages" not in record.pathname


def _setup_root_logger() -> None:
    global _SETUP_DONE
    if _SETUP_DONE:
        return
    _SETUP_DONE = True

    use_json = os.getenv("LOG_JSON", "false").lower() == "true"
    log_level = os.getenv("LOG_LEVEL", "DEBUG").upper()
    use_color = sys.stdout.isatty()

    root = logging.getLogger()
    root.setLevel(TRACE_LEVEL)

    # ── console — always human-readable colored text, never JSON ──
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, log_level, logging.DEBUG))
    console.setFormatter(RichFormatter(use_json=False, use_color=use_color))
    console.addFilter(_ExcludeSitePackagesFilter())

    root.addHandler(console)

    _LOG_DIR.mkdir(exist_ok=True)
    _ARCHIVE_DIR.mkdir(exist_ok=True)

    if use_json:
        # JSON files → picked up by Filebeat/Promtail → Elasticsearch/Loki → Kibana/Grafana
        _add_file_handler(root, _LOG_DIR / "app.json", TRACE_LEVEL, use_json=True)
        _add_file_handler(
            root, _LOG_DIR / "errors.json", logging.WARNING, use_json=True
        )
    else:
        # Plain text → good for local dev and docker logs
        _add_file_handler(root, _LOG_DIR / "app.log", TRACE_LEVEL, use_json=False)
        _add_file_handler(
            root, _LOG_DIR / "errors.log", logging.WARNING, use_json=False
        )

    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "hpack", "cerebras.cloud.sdk", "celery.utils.functional"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _add_file_handler(
    root: logging.Logger, path: Path, level: int, use_json: bool
) -> None:
    handler = RotatingFileHandler(
        path,
        maxBytes=10 * 1024 * 1024,  # 10 MB safety net between nightly rotations
        backupCount=2,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(RichFormatter(use_json=use_json, use_color=False))
    handler.addFilter(_ExcludeSitePackagesFilter())  # ← add this line
    root.addHandler(handler)


# ──────────────────────────────────────────────
# Public factory
# ──────────────────────────────────────────────
def get_logger(name: str) -> "AppLogger":
    """
    Call once at module level in every file:

        from once.logger import get_logger
        log = get_logger(__name__)
    """
    _setup_root_logger()
    return AppLogger(name)


# ──────────────────────────────────────────────
# AppLogger wrapper
# ──────────────────────────────────────────────
class AppLogger:
    def __init__(self, name: str):
        self._logger = logging.getLogger(name)

    # stacklevel=2 skips this wrapper so line numbers point to YOUR code
    def debug(self, msg, *args, **kwargs):
        self._logger.debug(msg, *args, stacklevel=2, **kwargs)

    def info(self, msg, *args, **kwargs):
        self._logger.info(msg, *args, stacklevel=2, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self._logger.warning(msg, *args, stacklevel=2, **kwargs)

    def error(self, msg, *args, **kwargs):
        self._logger.error(msg, *args, stacklevel=2, **kwargs)

    def critical(self, msg, *args, **kwargs):
        self._logger.critical(msg, *args, stacklevel=2, **kwargs)

    def exception(self, msg, *args, **kwargs):
        self._logger.exception(msg, *args, stacklevel=2, **kwargs)

    def trace(self, msg, *args, **kwargs):
        if self._logger.isEnabledFor(TRACE_LEVEL):
            self._logger.log(TRACE_LEVEL, msg, *args, stacklevel=2, **kwargs)

    def success(self, msg, *args, **kwargs):
        if self._logger.isEnabledFor(SUCCESS_LEVEL):
            self._logger.log(SUCCESS_LEVEL, msg, *args, stacklevel=2, **kwargs)

    def __getattr__(self, name):
        return getattr(self._logger, name)
