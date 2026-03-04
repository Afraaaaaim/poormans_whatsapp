"""
mcpserver/tools/users.py

Three tools:
  - add_user
  - deactivate_user
  - reactivate_user

Users are identified by phone number (preferred) or display name (fallback).
All errors returned as {"ok": False, "error": "..."} — nothing raises to the LLM.
"""

from __future__ import annotations

import logging
from typing import Any

from mcpserver.services.db import (
    db_list_all_users,
    db_get_user_by_phone,
    db_create_user,
    db_set_active,
)

log = logging.getLogger(__name__)

VALID_ROLES = {"owner", "admin", "user", "guest"}


# ── serializer ────────────────────────────────────────────────────────────────

def _row(u: Any) -> dict:
    return {
        "phone": u.phone,
        "display_name": u.display_name,
        "role": u.role,
        "is_active": u.is_active,
        "created_at": str(u.created_at),
    }


# ── lookup helper ─────────────────────────────────────────────────────────────

async def _resolve_user(
    phone: str | None,
    name: str | None,
) -> tuple[Any | None, str | None]:
    """
    Resolve a user by phone (preferred) or display name (fallback).

    Returns:
        (user_object, error_message)
        error_message is None on success.
    """
    if phone:
        user = await db_get_user_by_phone(phone)
        if user is None:
            return None, f"No user found with phone number {phone}."
        return user, None

    if name:
        all_users = await db_list_all_users()
        name_lower = name.strip().lower()
        matches = [u for u in all_users if u.display_name.strip().lower() == name_lower]

        if not matches:
            return None, f"No user found with the name '{name}'."
        if len(matches) > 1:
            numbers = ", ".join(u.phone for u in matches)
            return None, (
                f"Multiple users share the name '{name}' (phones: {numbers}). "
                "Please specify by phone number instead."
            )
        return matches[0], None

    return None, "Please provide either a phone number or a display name to identify the user."


# ── tools ─────────────────────────────────────────────────────────────────────

async def add_user(
    *,
    phone: str | None = None,
    name: str | None = None,
    role: str | None = None,
) -> dict:
    """
    Register a new user. phone, name, and role are all required.
    Returns a clear message listing whatever is missing.
    """
    missing = []
    if not phone:
        missing.append("phone")
    if not name:
        missing.append("name")
    if not role:
        missing.append("role")

    if missing:
        field_list = ", ".join(f"'{f}'" for f in missing)
        return {
            "ok": False,
            "error": f"Cannot add user — missing required field(s): {field_list}. Please provide them.",
            "missing_fields": missing,
        }

    if role not in VALID_ROLES:
        return {
            "ok": False,
            "error": f"'{role}' is not a valid role. Choose one of: {', '.join(sorted(VALID_ROLES))}.",
        }

    try:
        existing = await db_get_user_by_phone(phone)
        if existing:
            return {
                "ok": False,
                "error": (
                    f"A user with phone {phone} already exists "
                    f"(name: {existing.display_name}, role: {existing.role})."
                ),
            }

        user = await db_create_user(phone=phone, name=name, role=role)
        return {"ok": True, "user": _row(user)}

    except Exception as exc:
        log.exception("add_user failed")
        return {"ok": False, "error": f"Database error while adding user: {exc}"}


async def deactivate_user(
    *,
    phone: str | None = None,
    name: str | None = None,
) -> dict:
    """
    Deactivate a user (sets is_active=False).
    Identify by phone (preferred) or display name (fallback).
    """
    try:
        user, err = await _resolve_user(phone, name)
        if err:
            return {"ok": False, "error": err}

        if not user.is_active:
            return {
                "ok": False,
                "error": f"{user.display_name} ({user.phone}) is already inactive. No changes made.",
            }

        updated = await db_set_active(user.phone, active=False)
        if updated is None:
            return {"ok": False, "error": "User disappeared during update — please try again."}

        return {"ok": True, "user": _row(updated)}

    except Exception as exc:
        log.exception("deactivate_user failed")
        return {"ok": False, "error": f"Database error while deactivating user: {exc}"}


async def reactivate_user(
    *,
    phone: str | None = None,
    name: str | None = None,
) -> dict:
    """
    Reactivate a user (sets is_active=True).
    Identify by phone (preferred) or display name (fallback).
    """
    try:
        user, err = await _resolve_user(phone, name)
        if err:
            return {"ok": False, "error": err}

        if user.is_active:
            return {
                "ok": False,
                "error": f"{user.display_name} ({user.phone}) is already active. No changes made.",
            }

        updated = await db_set_active(user.phone, active=True)
        if updated is None:
            return {"ok": False, "error": "User disappeared during update — please try again."}

        return {"ok": True, "user": _row(updated)}

    except Exception as exc:
        log.exception("reactivate_user failed")
        return {"ok": False, "error": f"Database error while reactivating user: {exc}"}