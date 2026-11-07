"""Accounts prove their email with a code (DECISIONS #44)

Revision ID: 0084_email_verification
Revises: 0083_guest_messages
Create Date: 2026-11-06

- users.email_verified_at: when the account proved it reads its email — a
  code entered after sign-up, or a Google sign-in. Google-linked accounts are
  marked now: Google has already checked their address.
- email_codes: the 6-digit codes, hashed, one live per account.
"""
import sqlalchemy as sa

from alembic import op

revision = "0084_email_verification"
down_revision = "0083_guest_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE users SET email_verified_at = CURRENT_TIMESTAMP WHERE supabase_user_id IS NOT NULL")
    op.create_table(
        "email_codes",
        sa.Column("code_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_email_codes_user_id", "email_codes", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_email_codes_user_id", table_name="email_codes")
    op.drop_table("email_codes")
    op.drop_column("users", "email_verified_at")
