"""
mcpserver/services/db.py

DB adapter for MCP tools.
Only exposes what is needed: list users (for number-based lookup),
create, deactivate, and reactivate.

Phone numbers are stored digits-only (normalize_phone strips the leading +).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from once.db.models import UserModel
from once.db.session import AsyncSessionLocal
from once.db_services import DBService
from once.helper_functions import normalize_phone


# ── session helper ────────────────────────────────────────────────────────────

@asynccontextmanager
async def _session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── helpers ───────────────────────────────────────────────────────────────────

async def db_get_user_by_phone(phone: str) -> Any | None:
    """Pass-through to existing DBService."""
    return await DBService.get_user_by_phone(phone)


async def db_list_all_users() -> list[Any]:
    """
    Return ALL non-deleted users ordered by created_at ascending.
    This stable order is what we assign 1-based list numbers to.
    """
    async with _session() as session:
        result = await session.execute(
            select(UserModel)
            .where(UserModel.deleted_at.is_(None))
            .order_by(UserModel.created_at.asc())
        )
        return list(result.scalars().all())


async def db_create_user(phone: str, name: str, role: str) -> Any:
    """Insert a new user. Phone stored digits-only."""
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


async def db_set_active(phone: str, active: bool) -> Any | None:
    """
    Set is_active on a user identified by normalized phone.
    Returns the updated user, or None if not found.
    """
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
        user.is_active = active
        await session.flush()
        return user