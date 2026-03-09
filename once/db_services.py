"""
db_services.py — Database service layer

A singleton base class that holds one shared async engine + session factory.
Import and use it anywhere in the project without re-initializing.

Phone numbers: ALL phone inputs are normalized (+ stripped, country code
validated) via normalize_phone() before any DB read or write. Numbers are
stored as digit-only strings, e.g. "919562885142".

Usage:
    from once.db_services import DBService

    user = await DBService.get_user_by_phone("+919562885142")  # + is fine here
    user = await DBService.get_user_by_phone("919562885142")   # so is this
"""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from once.db.models import ConversationModel, ConversationParticipantModel, MessageModel, UserModel
from once.db.session import AsyncSessionLocal
from once.logger import get_logger, new_span
from once.helper_functions import normalize_phone

log = get_logger(__name__)


# ── Internal session helper ───────────────────────────────────────────────────


@asynccontextmanager
async def _session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── DBService ─────────────────────────────────────────────────────────────────


class DBService:
    """
    Stateless DB service. All methods are static — call directly:

        user = await DBService.get_user_by_phone("+91...")

    Phone numbers passed in any format (with or without +) are normalized
    automatically. Numbers are stored digit-only in the DB.
    """

    # ── USER QUERIES ──────────────────────────────────────────────────────────

    @staticmethod
    async def get_user_by_phone(phone: str) -> UserModel | None:
        """Look up an active user by phone. Accepts + prefix or plain digits."""
        with new_span("db.get_user_by_phone"):
            normalized = normalize_phone(phone)
            log.debug("Looking up user by phone: %s", normalized)
            async with _session() as session:
                result = await session.execute(
                    select(UserModel).where(
                        UserModel.phone == normalized,
                        UserModel.deleted_at.is_(None),
                    )
                )
                user = result.scalar_one_or_none()
                log.debug(
                    "User lookup %s: %s", normalized, "found" if user else "not found"
                )
                return user

    @staticmethod
    async def is_authorized(phone: str) -> bool:
        """
        Primary auth gate — returns True only if this number is active.
        Called on every inbound message before any processing.
        Accepts + prefix or plain digits.
        """
        with new_span("db.is_authorized"):
            try:
                normalized = normalize_phone(phone)
            except ValueError:
                log.warning("is_authorized: invalid phone format '%s' — denying", phone)
                return False

            log.debug("Auth check for: %s", normalized)
            async with _session() as session:
                result = await session.execute(
                    select(UserModel).where(
                        UserModel.phone == normalized,
                        UserModel.is_active == True,  # noqa: E712
                        UserModel.deleted_at.is_(None),
                    )
                )
                authorized = result.scalar_one_or_none() is not None
                log.debug("Auth result for %s: %s", normalized, authorized)
                return authorized

    @staticmethod
    async def get_owner() -> UserModel | None:
        """Fetch the single owner row."""
        with new_span("db.get_owner"):
            async with _session() as session:
                result = await session.execute(
                    select(UserModel).where(
                        UserModel.is_owner == True,  # noqa: E712
                        UserModel.deleted_at.is_(None),
                    )
                )
                return result.scalar_one_or_none()

    # ── CONVERSATION QUERIES ──────────────────────────────────────────────────

    @staticmethod
    async def get_default_conversation() -> ConversationModel | None:
        """Returns the seeded 'PA' conversation."""
        with new_span("db.get_default_conversation"):
            async with _session() as session:
                result = await session.execute(
                    select(ConversationModel).where(
                        ConversationModel.title == "PA",
                        ConversationModel.deleted_at.is_(None),
                    )
                )
                return result.scalar_one_or_none()

    @staticmethod
    async def get_conversation_by_waba_id(
        waba_chat_id: str,
    ) -> ConversationModel | None:
        """Look up a conversation by Meta's chat ID."""
        with new_span("db.get_conversation_by_waba_id"):
            async with _session() as session:
                result = await session.execute(
                    select(ConversationModel).where(
                        ConversationModel.waba_chat_id == waba_chat_id,
                        ConversationModel.deleted_at.is_(None),
                    )
                )
                return result.scalar_one_or_none()

    @staticmethod
    async def set_conversation_waba_id(
        conversation_id: uuid.UUID, waba_chat_id: str
    ) -> None:
        """Link Meta's chat ID to a conversation on first message arrival."""
        with new_span("db.set_conversation_waba_id"):
            log.debug(
                "Linking waba_chat_id=%s to conversation %s",
                waba_chat_id,
                conversation_id,
            )
            async with _session() as session:
                result = await session.execute(
                    select(ConversationModel).where(
                        ConversationModel.id == conversation_id
                    )
                )
                conv = result.scalar_one_or_none()
                if conv:
                    conv.waba_chat_id = waba_chat_id
                    log.success(
                        "Linked waba_chat_id to conversation %s", conversation_id
                    )
                else:
                    log.warning(
                        "Conversation %s not found for waba_chat_id link",
                        conversation_id,
                    )

    # ── MESSAGE QUERIES ───────────────────────────────────────────────────────

    @staticmethod
    async def save_message(
        conversation_id: uuid.UUID,
        direction: str,
        msg_type: str = "text",
        body: str | None = None,
        sender_id: uuid.UUID | None = None,
        waba_message_id: str | None = None,
        reply_to_waba_id: str | None = None,
        metadata: dict | None = None,
        is_llm_generated: bool = False,
        sender_type: str | None = None,
    ) -> MessageModel:
        """
        Persist a message. Works for both inbound and outbound.

        sender_type: 'llm' | 'human_owner' | 'human_user' | 'system'
        reply_to_waba_id: Meta wamid string — resolved to internal UUID automatically.
        """
        with new_span("db.save_message"):
            log.debug(
                "Saving message direction=%s type=%s llm=%s waba_id=%s",
                direction,
                msg_type,
                is_llm_generated,
                waba_message_id,
            )
            async with _session() as session:
                # Resolve reply threading
                reply_to_id: uuid.UUID | None = None
                if reply_to_waba_id:
                    ref = await session.execute(
                        select(MessageModel).where(
                            MessageModel.waba_message_id == reply_to_waba_id
                        )
                    )
                    ref_msg = ref.scalar_one_or_none()
                    if ref_msg:
                        reply_to_id = ref_msg.id
                    else:
                        log.warning(
                            "reply_to waba_id=%s not found — thread link skipped",
                            reply_to_waba_id,
                        )

                msg = MessageModel(
                    id=uuid.uuid4(),
                    conversation_id=conversation_id,
                    sender_id=sender_id,
                    waba_message_id=waba_message_id,
                    direction=direction,
                    msg_type=msg_type,
                    status="delivered" if direction == "inbound" else "pending",
                    body=body,
                    reply_to_id=reply_to_id,
                    metadata_=metadata or {},
                    is_llm_generated=is_llm_generated,
                    sender_type=sender_type,
                )
                session.add(msg)
                await session.flush()
                log.success(
                    "Saved message id=%s direction=%s sender_type=%s",
                    msg.id,
                    direction,
                    sender_type,
                )
                return msg

    @staticmethod
    async def get_message_by_waba_id(waba_message_id: str) -> MessageModel | None:
        """Fetch a message by Meta's wamid."""
        with new_span("db.get_message_by_waba_id"):
            async with _session() as session:
                result = await session.execute(
                    select(MessageModel).where(
                        MessageModel.waba_message_id == waba_message_id
                    )
                )
                return result.scalar_one_or_none()

    @staticmethod
    async def update_message_status(waba_message_id: str, status: str) -> bool:
        """
        Update delivery status using Meta's wamid.
        Stamps sent_at / delivered_at / read_at automatically.
        Returns True if found and updated, False if not found.
        """
        with new_span("db.update_message_status"):
            log.debug("Status update waba_id=%s → %s", waba_message_id, status)
            async with _session() as session:
                result = await session.execute(
                    select(MessageModel).where(
                        MessageModel.waba_message_id == waba_message_id
                    )
                )
                msg = result.scalar_one_or_none()
                if not msg:
                    log.warning(
                        "update_message_status: waba_id=%s not found", waba_message_id
                    )
                    return False

                now = datetime.now(timezone.utc)
                msg.status = status
                if status == "sent":
                    msg.sent_at = now
                elif status == "delivered":
                    msg.delivered_at = now
                elif status == "read":
                    msg.read_at = now

                log.success("Updated %s → %s", waba_message_id, status)
                return True

    @staticmethod
    async def get_conversation_history(
        conversation_id: uuid.UUID,
        limit: int = 50,
    ) -> list[MessageModel]:
        """
        Fetch the last N messages oldest-first.
        Used to build LLM context or display chat history.
        """
        with new_span("db.get_conversation_history"):
            log.debug(
                "Fetching last %d messages for conversation %s", limit, conversation_id
            )
            async with _session() as session:
                result = await session.execute(
                    select(MessageModel)
                    .where(
                        MessageModel.conversation_id == conversation_id,
                        MessageModel.deleted_at.is_(None),
                    )
                    .order_by(MessageModel.created_at.desc())
                    .limit(limit)
                )
                messages = list(reversed(result.scalars().all()))
                log.debug("Fetched %d messages", len(messages))
                return messages

    @staticmethod
    async def bulk_save_messages(messages: list[dict]) -> int:
        """
        Insert multiple message rows in one transaction.
        Used by the Celery flush task when writing Redis history to DB.
        Returns count of rows inserted.
        """
        with new_span("db.bulk_save_messages"):
            log.debug("Bulk saving %d messages", len(messages))
            async with _session() as session:
                rows = [
                    MessageModel(
                        id=uuid.uuid4(),
                        conversation_id=m["conversation_id"],
                        sender_id=m.get("sender_id"),
                        waba_message_id=m.get("waba_message_id"),
                        direction=m["direction"],
                        msg_type=m.get("msg_type", "text"),
                        status=m.get("status", "delivered"),
                        body=m.get("body"),
                        metadata_=m.get("metadata", {}),
                        is_llm_generated=m.get("is_llm_generated", False),
                        sender_type=m.get("sender_type"),
                    )
                    for m in messages
                ]
                session.add_all(rows)
                await session.flush()
                log.success("Bulk saved %d messages", len(rows))
                return len(rows)

    @staticmethod
    async def update_message_waba_id(message_id: uuid.UUID, waba_message_id: str) -> None:
        """Stamp the Meta wamid onto an outbound row after send."""
        async with _session() as session:
            result = await session.execute(
                select(MessageModel).where(MessageModel.id == message_id)
            )
            msg = result.scalar_one_or_none()
            if msg:
                msg.waba_message_id = waba_message_id
                await session.flush()  # 👈 this was missing
                log.debug("Patched waba_id=%s onto message id=%s", waba_message_id, message_id)
            else:
                log.warning("update_message_waba_id: message id=%s not found", message_id)

    @staticmethod
    async def get_or_create_conversation(from_number: str, user_id: uuid.UUID) -> ConversationModel:
        """
        Get existing conversation for this phone number or create a new one.
        Also creates a ConversationParticipantModel row on first creation.
        """
        with new_span("db.get_or_create_conversation"):
            normalized = normalize_phone(from_number)
            async with _session() as session:
                # Try to find existing conversation for this phone
                result = await session.execute(
                    select(ConversationModel).where(
                        ConversationModel.waba_chat_id == normalized,
                        ConversationModel.deleted_at.is_(None),
                    )
                )
                conversation = result.scalar_one_or_none()

                if conversation:
                    log.debug("Found existing conversation for %s id=%s", normalized, conversation.id)
                    return conversation

                # Create new conversation + participant
                log.debug("Creating new conversation for %s", normalized)
                conversation = ConversationModel(
                    id=uuid.uuid4(),
                    waba_chat_id=normalized,
                    is_group=False,
                )
                session.add(conversation)
                await session.flush()  # get conversation.id before participant insert

                participant = ConversationParticipantModel(
                    id=uuid.uuid4(),
                    conversation_id=conversation.id,
                    user_id=user_id,
                    is_admin=False,
                )
                session.add(participant)
                await session.flush()

                log.success("Created conversation id=%s for %s", conversation.id, normalized)
                return conversation