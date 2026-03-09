"""
Run with:  python -m seeds.seed

Seeds:
    1. Owner user      — the WABA account (PHONE_NUMBER + BA_DISPLAY_NAME)
    2. Admin user      — your personal number (ADMIN_PHONE + ADMIN_DISPLAY_NAME)
    3. PA conversation — default conversation both are participants of

Phone numbers from env are normalized (+ stripped, country code validated)
before being written to the DB.

ENV VARS required:
    PHONE_NUMBER         — owner E.164 phone (the WABA number)
    BA_DISPLAY_NAME   — display name for owner  (default: "Me")
    ADMIN_PHONE          — your personal E.164 phone
    ADMIN_DISPLAY_NAME   — display name for admin  (default: "Admin")
"""

import asyncio
import os
import uuid

from dotenv import load_dotenv

load_dotenv()

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import pool, select
from sqlalchemy.ext.asyncio import create_async_engine

from once.db.models import ConversationModel, ConversationParticipantModel, UserModel
from once.db.session import AsyncSessionLocal
from once.helper_functions import normalize_phone

DATABASE_URL = os.getenv("DATABASE_URL", "")

# Normalize at read time — everything from here on is digit-only
try:
    OWNER_PHONE = normalize_phone(os.getenv("BA_PHONE_NUMBER", ""))
except ValueError as e:
    raise RuntimeError(f"Invalid BA_PHONE_NUMBER in .env: {e}") from e

try:
    ADMIN_PHONE = normalize_phone(os.getenv("ADMIN_PHONE", ""))
except ValueError as e:
    raise RuntimeError(f"Invalid ADMIN_PHONE in .env: {e}") from e

OWNER_NAME = os.getenv("BA_DISPLAY_NAME", "Me")
ADMIN_NAME = os.getenv("ADMIN_DISPLAY_NAME", "Admin")


# ── Migration guard ───────────────────────────────────────────────────────────

async def check_migrations_current() -> bool:
    engine = create_async_engine(DATABASE_URL, poolclass=pool.NullPool)
    try:
        async with engine.connect() as conn:
            def _get_current(sync_conn):
                ctx = MigrationContext.configure(sync_conn)
                return ctx.get_current_heads()
            current_heads = await conn.run_sync(_get_current)

        alembic_cfg = Config("alembic.ini")
        script = ScriptDirectory.from_config(alembic_cfg)
        expected = set(script.get_heads())
        current  = set(current_heads)

        if not current:
            print("[seed] No migrations applied yet — run `alembic upgrade head` first.")
            return False
        if current != expected:
            print(f"[seed] Migration mismatch.\n       Current : {current}\n       Expected: {expected}")
            print("       Run `alembic upgrade head` to resolve.")
            return False
        return True
    finally:
        await engine.dispose()


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_or_create_user(
    session,
    phone: str,           # already normalized
    display_name: str,
    role: str,
    is_owner: bool,
) -> tuple[UserModel, bool]:
    result = await session.execute(
        select(UserModel).where(UserModel.phone == phone)
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing, False

    user = UserModel(
        id=uuid.uuid4(),
        phone=phone,
        display_name=display_name,
        role=role,
        is_owner=is_owner,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user, True


async def _add_participant_if_missing(
    session,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    is_admin: bool,
) -> bool:
    result = await session.execute(
        select(ConversationParticipantModel).where(
            ConversationParticipantModel.conversation_id == conversation_id,
            ConversationParticipantModel.user_id == user_id,
        )
    )
    if result.scalar_one_or_none():
        return False

    session.add(ConversationParticipantModel(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        user_id=user_id,
        is_admin=is_admin,
    ))
    return True


# ── Main seed ─────────────────────────────────────────────────────────────────

async def seed() -> None:
    if OWNER_PHONE == ADMIN_PHONE:
        raise RuntimeError(
            "PHONE_NUMBER and ADMIN_PHONE cannot be the same. "
            "Owner is the WABA account, admin is your personal number."
        )

    if not await check_migrations_current():
        raise SystemExit(1)

    async with AsyncSessionLocal() as session:

        # ── 1. Owner ─────────────────────────────────────────────────────────
        owner, owner_created = await _get_or_create_user(
            session, OWNER_PHONE, OWNER_NAME, role="owner", is_owner=True
        )
        print(
            f"[seed] {'Created' if owner_created else 'Exists'} owner: "
            f"{owner.phone} ({owner.display_name})"
        )

        # ── 2. Admin ─────────────────────────────────────────────────────────
        admin, admin_created = await _get_or_create_user(
            session, ADMIN_PHONE, ADMIN_NAME, role="admin", is_owner=False
        )
        print(
            f"[seed] {'Created' if admin_created else 'Exists'} admin: "
            f"{admin.phone} ({admin.display_name})"
        )

        # ── 3. PA conversation ────────────────────────────────────────────────
        conv_result = await session.execute(
            select(ConversationModel).where(ConversationModel.title == "PA")
        )
        conversation = conv_result.scalar_one_or_none()

        if conversation:
            print("[seed] Conversation 'PA' already exists — skipping creation.")
        else:
            conversation = ConversationModel(id=uuid.uuid4(), title="PA", is_group=False)
            session.add(conversation)
            await session.flush()
            print("[seed] Created conversation 'PA'.")

        # ── 4. Participants ───────────────────────────────────────────────────
        owner_added = await _add_participant_if_missing(
            session, conversation.id, owner.id, is_admin=True
        )
        admin_added = await _add_participant_if_missing(
            session, conversation.id, admin.id, is_admin=True
        )

        print(f"[seed] Owner in PA:  {'added' if owner_added else 'already present'}")
        print(f"[seed] Admin in PA:  {'added' if admin_added else 'already present'}")

        await session.commit()
        print("[seed] Done.")


if __name__ == "__main__":
    asyncio.run(seed())