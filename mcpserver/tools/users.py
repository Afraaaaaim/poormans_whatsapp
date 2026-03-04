"""
MCP tools — users category.

All tools here are registered in _registry.py.
Permission checks are enforced by the MCP server before dispatch.
DB access goes through once.db_services (re-exported from mcp/services/db.py).
"""

from __future__ import annotations

import logging
from typing import Any

from mcpserver.services.db import (
    db_get_user_by_phone,
    db_get_user_by_id,
    db_list_users,
    db_create_user,
    db_update_user_role,
    db_delete_user,
)

log = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _user_row(u: Any) -> dict:
    """Serialise a DB user row to a plain dict."""
    return {
        "id": str(u.id),
        "phone": u.phone,
        "display_name": u.display_name,
        "role": u.role,
        "is_active": u.is_active,
        "created_at": str(u.created_at),
    }


# ── tools ─────────────────────────────────────────────────────────────────────

async def get_user(
    *,
    phone: str | None = None,
    user_id: int | None = None,
) -> dict:
    """
    Fetch a user by phone number OR user_id.
    At least one of the two must be supplied.

    Returns:
        {"ok": True, "user": {...}} on success
        {"ok": False, "error": "..."} on failure
    """
    if phone is None and user_id is None:
        return {"ok": False, "error": "Provide either 'phone' or 'user_id'."}

    try:
        if phone:
            user = await db_get_user_by_phone(phone)
        else:
            user = await db_get_user_by_id(user_id)

        if user is None:
            return {"ok": False, "error": "User not found."}
        return {"ok": True, "user": _user_row(user)}
    except Exception as exc:
        log.exception("get_user failed")
        return {"ok": False, "error": str(exc)}


async def list_users(*, role_filter: str | None = None) -> dict:
    """
    List all users, optionally filtered by role.

    Args:
        role_filter: one of 'owner' | 'admin' | 'user' | 'guest' (optional)

    Returns:
        {"ok": True, "users": [...], "count": N}
    """
    try:
        users = await db_list_users(role_filter=role_filter)
        return {
            "ok": True,
            "users": [_user_row(u) for u in users],
            "count": len(users),
        }
    except Exception as exc:
        log.exception("list_users failed")
        return {"ok": False, "error": str(exc)}


async def add_user(*, phone: str, name: str, role: str = "user") -> dict:
    """
    Register a new user.

    Args:
        phone: E.164 phone number e.g. +919876543210
        name:  Display name
        role:  'owner' | 'admin' | 'user' | 'guest'  (default: 'user')

    Returns:
        {"ok": True, "user": {...}} or {"ok": False, "error": "..."}
    """
    valid_roles = {"owner", "admin", "user", "guest"}
    if role not in valid_roles:
        return {"ok": False, "error": f"Invalid role '{role}'. Choose from {valid_roles}."}

    try:
        existing = await db_get_user_by_phone(phone)
        if existing:
            return {"ok": False, "error": f"User with phone {phone} already exists."}

        user = await db_create_user(phone=phone, name=name, role=role)
        return {"ok": True, "user": _user_row(user)}
    except Exception as exc:
        log.exception("add_user failed")
        return {"ok": False, "error": str(exc)}


async def update_user_role(*, phone: str, new_role: str) -> dict:
    """
    Change a user's role.

    Args:
        phone:    E.164 phone number
        new_role: 'owner' | 'admin' | 'user' | 'guest'

    Returns:
        {"ok": True, "user": {...}} or {"ok": False, "error": "..."}
    """
    valid_roles = {"owner", "admin", "user", "guest"}
    if new_role not in valid_roles:
        return {"ok": False, "error": f"Invalid role '{new_role}'. Choose from {valid_roles}."}

    try:
        user = await db_update_user_role(phone=phone, new_role=new_role)
        if user is None:
            return {"ok": False, "error": f"User with phone {phone} not found."}
        return {"ok": True, "user": _user_row(user)}
    except Exception as exc:
        log.exception("update_user_role failed")
        return {"ok": False, "error": str(exc)}


async def remove_user(*, phone: str) -> dict:
    """
    Permanently delete a user.

    Args:
        phone: E.164 phone number

    Returns:
        {"ok": True, "deleted": phone} or {"ok": False, "error": "..."}
    """
    try:
        deleted = await db_delete_user(phone=phone)
        if not deleted:
            return {"ok": False, "error": f"User with phone {phone} not found."}
        return {"ok": True, "deleted": phone}
    except Exception as exc:
        log.exception("remove_user failed")
        return {"ok": False, "error": str(exc)}