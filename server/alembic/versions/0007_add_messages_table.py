"""add messages table

Revision ID: 0007_add_messages
Revises: 0006_add_is_admin
Create Date: 2026-05-11
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_add_messages"
down_revision = "0006_add_is_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "messages",
        sa.Column("message_id", sa.String(36), primary_key=True),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.booking_id"), nullable=False, index=True),
        sa.Column("sender_id", sa.String(36), sa.ForeignKey("users.user_id"), nullable=False, index=True),
        sa.Column("receiver_id", sa.String(36), sa.ForeignKey("users.user_id"), nullable=False, index=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("is_read", sa.Boolean, nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_table("messages")
