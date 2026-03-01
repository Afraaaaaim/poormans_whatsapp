"""add sender_type and is_llm_generated to messages

Revision ID: 0002
Revises: 4fa3cdf9e257
Create Date: 2026-03-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "4fa3cdf9e257"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enum for who authored an outbound message
    sender_type_enum = sa.Enum(
        "llm",           # written by the LLM
        "human_owner",   # you replied manually via your personal number
        "human_user",    # a real user (future multi-user)
        "system",        # automated system message
        name="sender_type_enum",
    )
    sender_type_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "messages",
        sa.Column(
            "sender_type",
            sender_type_enum,
            nullable=True,   # nullable — inbound messages won't have this
        ),
    )
    op.add_column(
        "messages",
        sa.Column(
            "is_llm_generated",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("messages", "is_llm_generated")
    op.drop_column("messages", "sender_type")
    sa.Enum(name="sender_type_enum").drop(op.get_bind(), checkfirst=True)