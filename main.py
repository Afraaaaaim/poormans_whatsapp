# main.py

import json as _json
import os
import sys

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

load_dotenv(".env", override=True)
from once.otel_setup import setup_otel
setup_otel()

from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from whatsapp import AsyncWhatsApp, get_mobile

from once.logger import get_logger, set_request_context
from once.once import handle_inbound_message, handle_status_update
from once.redis_service import RedisService
from once.utils import normalize_phone

# =========================
# Load Environment
# =========================
logger = get_logger(__name__)


# =========================
# Constants
# =========================
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", 8000))

WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
BA_PHONE_NUMBER = os.getenv("BA_PHONE_NUMBER")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
CUSTOM_ENDPOINT = os.getenv("CUSTOM_ENDPOINT")

LOGGER = os.getenv("LOGGER")
DEBUG = os.getenv("DEBUG")
VERSION = os.getenv("VERSION")
UPDATE_CHECK = os.getenv("UPDATE_CHECK")

# =========================
# Validation
# =========================
MANDATORY_VARS = {
    "WHATSAPP_ACCESS_TOKEN": WHATSAPP_ACCESS_TOKEN,
    "WHATSAPP_VERIFY_TOKEN": WHATSAPP_VERIFY_TOKEN,
    "BA_PHONE_NUMBER": BA_PHONE_NUMBER,
    "PHONE_NUMBER_ID": PHONE_NUMBER_ID,
    "CUSTOM_ENDPOINT": CUSTOM_ENDPOINT,
}

missing = [key for key, value in MANDATORY_VARS.items() if not value]

if missing:
    logger.exception(f"Missing mandatory environment variables: {', '.join(missing)}")
    sys.exit(1)


# Normalize booleans safely
def to_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() == "true"


LOGGER = to_bool(LOGGER)
DEBUG = to_bool(DEBUG)
UPDATE_CHECK = to_bool(UPDATE_CHECK)

# ── Webhook handlers — no logic here, just extract + hand off ─────────────────

# =========================
# App Initialization
# =========================
try:
    wa = AsyncWhatsApp(
        token=WHATSAPP_ACCESS_TOKEN,
        phone_number_id={BA_PHONE_NUMBER: PHONE_NUMBER_ID},
        verify_token=WHATSAPP_VERIFY_TOKEN,
        logger=LOGGER,
        debug=DEBUG,
        version=VERSION,
        update_check=UPDATE_CHECK,
    )
except Exception as e:
    logger.exception(f"Failed to initialize WhatsApp client: {e}")
    sys.exit(1)

app = FastAPI()
FastAPIInstrumentor().instrument_app(app)  # ← add this line
try:
    app.mount(CUSTOM_ENDPOINT, wa.app)
except Exception as e:
    logger.exception(f"Failed to mount WhatsApp endpoint: {e}")
    sys.exit(1)



@wa.app.middleware("http")
async def intercept_status_updates(request: Request, call_next):
    if request.method == "POST":
        body = await request.body()
        try:
            data = _json.loads(body)
            changed = data.get("entry", [{}])[0].get("changes", [{}])[0].get("field")
            if changed == "messages":
                statuses = (
                    data.get("entry", [{}])[0]
                    .get("changes", [{}])[0]
                    .get("value", {})
                    .get("statuses", [])
                )
                if statuses:
                    set_request_context()
                    for status_obj in statuses:
                        waba_message_id = status_obj.get("id")
                        status_value = status_obj.get("status")
                        if not waba_message_id or not status_value:
                            continue

                        dedup_key = f"{waba_message_id}:{status_value}"
                        if await RedisService.is_duplicate_message(dedup_key):
                            logger.debug(
                                "Duplicate status %s:%s — discarding",
                                waba_message_id,
                                status_value,
                            )
                            continue

                        logger.debug("Status update: %s → %s", waba_message_id, status_value)
                        await handle_status_update(waba_message_id, status_value)

                    return JSONResponse({"success": True})  # ✅ same as before

        except Exception:
            logger.exception("intercept_status_updates: failed to parse body")
            return JSONResponse({"success": True})  # ✅ always ACK even on error
            # Meta doesn't need to know about our internal failures

        # Reconstruct for non-status webhooks (message inbound etc.)
        async def receive():
            return {"type": "http.request", "body": body}
        request._receive = receive

    return await call_next(request)


@wa.on_message
async def on_message(message):
    set_request_context()

    waba_message_id = getattr(message, "id", None)

    # Dedup via Redis — atomic, persistent, TTL-bounded
    if waba_message_id and await RedisService.is_duplicate_message(waba_message_id):
        logger.debug("Duplicate message %s — discarding", waba_message_id)
        return

    from_number = get_mobile(message.data)
    if not from_number:
        logger.error("Could not extract phone number from message data")
        return

    # Discard messages sent by the bot itself — Meta echoes our outbound messages back
    owner_phone = normalize_phone(os.getenv("BA_PHONE_NUMBER", ""))
    if from_number == owner_phone:
        logger.debug("Ignoring echo of own message from %s", from_number)
        return

    logger.info("Inbound message received")
    logger.debug(
        "RAW WEBHOOK: wamid=%s body=%s timestamp=%s",
        getattr(message, "id", None),
        getattr(message, "content", None),
        message.data.get("entry", [{}])[0]
        .get("changes", [{}])[0]
        .get("value", {})
        .get("messages", [{}])[0]
        .get("timestamp"),
    )
    msg_type = getattr(message, "type", "text")
    body = getattr(message, "content", None) or getattr(message, "body", None)
    reply_to_waba_id = None
    logger.debug("Extracted body: %r type: %s", body, msg_type)

    context = getattr(message, "context", None)
    if context:
        reply_to_waba_id = getattr(context, "id", None)

    await handle_inbound_message(
        wa=wa,
        from_number=from_number,
        waba_message_id=waba_message_id,
        msg_type=msg_type,
        body=body,
        reply_to_waba_id=reply_to_waba_id,
        raw_metadata=message.data if hasattr(message, "data") else {},
    )


# =========================
# Entry Point
# =========================
if __name__ == "__main__":
    try:
        uvicorn.run(
            app,
            host=HOST,
            port=PORT,
            reload=False,
        )
    except Exception as e:
        print(f"Failed to start server: {e}")
        sys.exit(1)
