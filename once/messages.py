import os

ADMIN_DISPLAY_NAME = os.getenv("ADMIN_DISPLAY_NAME","Admin")

REJECTION_MESSAGES = {
    "not_found": (
        "👋 *Hey there!*\n\n"
        "It looks like you don't have access to this assistant yet.\n\n"
        f"📩 Reach out to *{ADMIN_DISPLAY_NAME}* to get added!"
    ),
    "inactive": (
        "😴 *Your account is currently inactive.*\n\n"
        "Looks like your access has been paused for now.\n\n"
        "📩 Drop a message to the admin and they'll get you sorted!"
    ),
    "deleted": (
        "💔 *Your account has been removed.*\n\n"
        "It seems your access has been revoked.\n\n"
        "📩 If you think this is a mistake, contact the admin."
    ),
}