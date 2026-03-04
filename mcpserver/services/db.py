"""
mcp/services/db.py

Adapter layer — MCP tools import DB helpers from here.
Wraps DBService static methods + implements missing ones
(get_user_by_id, list_users, create_user, update_user_role, delete_user).

Phone numbers: stored digits-only (normalize_phone strips +), consistent
with existing data. The E.164 DB constraint should be updated to reflect this.

TODO: migrate the new methods into once/db_services.py DBService when stable.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from once.db.models import UserModel
from once.db.session import AsyncSessionLocal
from once.db_services import DBService
from once.utils import normalize_phone


# ── Session helper ────────────────────────────────────────────────────────────

@asynccontextmanager
async def _session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Pass-through wrappers for existing DBService methods ─────────────────────

async def db_get_user_by_phone(phone: str) -> Any | None:
    return await DBService.get_user_by_phone(phone)


async def db_is_authorized(phone: str) -> bool:
    return await DBService.is_authorized(phone)


# ── New methods (not yet in DBService) ───────────────────────────────────────
# TODO: migrate these into once/db_services.py when stable.

async def db_get_user_by_id(user_id: int | uuid.UUID) -> Any | None:
    """Fetch a user by primary key."""
    async with _session() as session:
        result = await session.execute(
            select(UserModel).where(
                UserModel.id == user_id,
                UserModel.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()


async def db_list_users(role_filter: str | None = None) -> list[Any]:
    """Return all non-deleted users, optionally filtered by role."""
    async with _session() as session:
        q = select(UserModel).where(UserModel.deleted_at.is_(None))
        if role_filter:
            q = q.where(UserModel.role == role_filter)
        result = await session.execute(q.order_by(UserModel.id))
        return list(result.scalars().all())


async def db_create_user(phone: str, name: str, role: str) -> Any:
    """Insert a new user row. Phone stored as digits-only (normalize_phone strips +)."""
    async with _session() as session:
        normalized = normalize_phone(phone)
        user = UserModel(
            id=uuid.uuid4(),
            phone=normalized,
            display_name=name,
            role=role,
            is_active=True,
            is_owner=(role == "owner"),
        )
        session.add(user)
        await session.flush()
        return user


async def db_update_user_role(phone: str, new_role: str) -> Any | None:
    """Update a user's role. Returns updated user or None if not found."""
    async with _session() as session:
        normalized = normalize_phone(phone)
        result = await session.execute(
            select(UserModel).where(
                UserModel.phone == normalized,
                UserModel.deleted_at.is_(None),
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            return None
        user.role = new_role
        user.is_owner = (new_role == "owner")
        await session.flush()
        return user


async def db_delete_user(phone: str) -> bool:
    """Soft-delete by phone. Returns True if found, False if not."""
    async with _session() as session:
        normalized = normalize_phone(phone)
        result = await session.execute(
            select(UserModel).where(
                UserModel.phone == normalized,
                UserModel.deleted_at.is_(None),
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            return False
        user.deleted_at = datetime.now(timezone.utc)
        await session.flush()
        return True
