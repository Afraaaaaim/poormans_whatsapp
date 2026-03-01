"""fix phone constraint to digit-only no plus

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-02
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the old E.164 constraint that required a leading +
    op.drop_constraint("users_phone_e164", "users", type_="check")

    # Strip + from any existing rows before adding the new constraint
    op.execute("UPDATE users SET phone = LTRIM(phone, '+')")

    # New constraint: digits only, 7–15 chars, no leading zero (country code present)
    op.create_check_constraint(
        "users_phone_digits_only",
        "users",
        r"phone ~ '^[1-9]\d{6,14}$'",
    )


def downgrade() -> None:
    op.drop_constraint("users_phone_digits_only", "users", type_="check")
    op.create_check_constraint(
        "users_phone_e164",
        "users",
        r"phone ~ '^\+[1-9]\d{6,14}$'",
    )