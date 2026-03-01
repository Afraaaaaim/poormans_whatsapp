"""
models/message.py — Message ORM model
"""

import uuid

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from once.db.base import Base

# Must match the enum created in the migration exactly — same name, same values.
# create_type=False tells SQLAlchemy not to try to CREATE it (migration already did).
sender_type_enum = ENUM(
    "llm",
    "human_owner",
    "human_user",
    "system",
    name="sender_type_enum",
    create_type=False,
)


class MessageModel(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    sender_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    waba_message_id: Mapped[str | None] = mapped_column(
        Text, unique=True, nullable=True
    )
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    msg_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="text")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )

    # ── Who authored this message ──────────────────────────────────────────
    is_llm_generated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    sender_type: Mapped[str | None] = mapped_column(
        sender_type_enum,  # ← explicit PG enum, not Text
        nullable=True,
    )

    # ── Delivery timestamps ────────────────────────────────────────────────
    sent_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    read_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Relationships ──────────────────────────────────────────────────────
    conversation: Mapped["ConversationModel"] = relationship(
        "ConversationModel", back_populates="messages"
    )  # noqa: F821
    sender: Mapped["UserModel"] = relationship(
        "UserModel", back_populates="messages"
    )  # noqa: F821
    reply_to: Mapped["MessageModel | None"] = relationship(
        "MessageModel", remote_side="MessageModel.id", foreign_keys=[reply_to_id]
    )
    media: Mapped[list["MessageMediaModel"]] = relationship(
        "MessageMediaModel", back_populates="message"
    )  # noqa: F821

    __table_args__ = (
        CheckConstraint(
            "direction IN ('inbound', 'outbound')", name="msg_direction_valid"
        ),
        CheckConstraint(
            "msg_type IN ('text','image','video','audio','document','sticker','location','contact','reaction','system')",
            name="msg_type_valid",
        ),
        CheckConstraint(
            "status IN ('pending','sent','delivered','read','failed')",
            name="msg_status_valid",
        ),
        CheckConstraint(
            "body IS NOT NULL OR msg_type != 'text'", name="msg_has_content"
        ),
    )
