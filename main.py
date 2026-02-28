# main.py

import os
import sys
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from whatsapp import AsyncWhatsApp

# =========================
# Load Environment
# =========================
load_dotenv(".env", override=True)

# =========================
# Constants
# =========================
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", 8000))
                 
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")
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
    "PHONE_NUMBER": PHONE_NUMBER,
    "PHONE_NUMBER_ID": PHONE_NUMBER_ID,
    "CUSTOM_ENDPOINT": CUSTOM_ENDPOINT,
}

missing = [key for key, value in MANDATORY_VARS.items() if not value]

if missing:
    print(f"Missing mandatory environment variables: {', '.join(missing)}")
    sys.exit(1)

# Normalize booleans safely
def to_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() == "true"

LOGGER = to_bool(LOGGER)
DEBUG = to_bool(DEBUG)
UPDATE_CHECK = to_bool(UPDATE_CHECK)

# =========================
# App Initialization
# =========================
try:
    wa = AsyncWhatsApp(
        token=WHATSAPP_ACCESS_TOKEN,
        phone_number_id={PHONE_NUMBER: PHONE_NUMBER_ID},
        verify_token=WHATSAPP_VERIFY_TOKEN,
        logger=LOGGER,
        debug=DEBUG,
        version=VERSION,
        update_check=UPDATE_CHECK,
    )
except Exception as e:
    print(f"Failed to initialize WhatsApp client: {e}")
    sys.exit(1)

app = FastAPI()

try:
    app.mount(CUSTOM_ENDPOINT, wa.app)
except Exception as e:
    print(f"Failed to mount WhatsApp endpoint: {e}")
    sys.exit(1)

@wa.on_message
async def on_message(message):
    print(message)



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