"""
utils.py — Shared utilities
=============================
Location: once/utils.py

Usage:
    from once.utils import normalize_phone

    normalize_phone("+919562885142")  → "919562885142"
    normalize_phone("919562885142")   → "919562885142"
    normalize_phone("+12125551234")   → "12125551234"
    normalize_phone("0919562885142")  → raises ValueError (leading zero after strip)
    normalize_phone("123")            → raises ValueError (too short)
    normalize_phone("")               → raises ValueError
"""

import re

from once.logger import get_logger

log = get_logger(__name__)

# After stripping +, a valid number is:
#   - 7 to 15 digits only (E.164 range without the +)
#   - Does NOT start with 0 (that would mean missing/wrong country code)
_PHONE_RE = re.compile(r"^[1-9]\d{6,14}$")


def normalize_phone(phone: str) -> str:
    """
    Normalize a phone number to digit-only format with country code.

    Rules:
        1. Strip whitespace
        2. Strip leading +
        3. Reject if empty
        4. Reject if starts with 0 (no leading zeros — means country code is missing)
        5. Reject if not 7–15 digits (E.164 range)
        6. Reject if contains any non-digit characters after stripping +

    Returns the normalized digit-only string, e.g. "919562885142".
    Raises ValueError with a clear message on any violation.

    Examples:
        "+919562885142" → "919562885142"
        "919562885142"  → "919562885142"
        "+12125551234"  → "12125551234"
        "0919562885142" → ValueError
        "+44 7700 900123"→ ValueError (spaces not allowed)
        "123"           → ValueError (too short)
    """
    if not phone:
        raise ValueError("Phone number cannot be empty.")

    normalized = phone.strip()

    # Strip leading +
    if normalized.startswith("+"):
        normalized = normalized[1:]

    # No spaces, dashes, or other separators allowed
    if not normalized.isdigit():
        raise ValueError(
            f"Phone number '{phone}' contains non-digit characters after stripping '+'. "
            "Remove spaces, dashes, and brackets before storing."
        )

    # Must not start with 0 — that means the country code is missing
    if normalized.startswith("0"):
        raise ValueError(
            f"Phone number '{phone}' starts with 0 after stripping '+'. "
            "This usually means the country code is missing (e.g. use '919...' not '09...')."
        )

    # Length check (E.164 without +: 7–15 digits)
    if not _PHONE_RE.match(normalized):
        raise ValueError(
            f"Phone number '{phone}' is not a valid E.164 number. "
            f"Expected 7–15 digits starting with country code, got '{normalized}' ({len(normalized)} digits)."
        )

    log.debug("normalize_phone: '%s' → '%s'", phone, normalized)
    return normalized
