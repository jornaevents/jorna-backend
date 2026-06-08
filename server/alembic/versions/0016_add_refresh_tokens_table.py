"""add refresh_tokens table

Revision ID: 0016_refresh_tokens
Revises: 0015_move_event_name
Create Date: 2026-06-08
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_refresh_tokens"
down_revision = "0015_move_event_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("token_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id"), nullable=False, index=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("family", sa.String(36), nullable=False, index=True),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("refresh_tokens")
