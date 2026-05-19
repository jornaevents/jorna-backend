"""add conversations and group messaging tables

Revision ID: 0010_add_conversations
Revises: 0009_add_bundles
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_add_conversations"
down_revision = "0009_add_bundles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("conversation_id", sa.String(36), primary_key=True),
        sa.Column("bundle_id", sa.String(36), sa.ForeignKey("bundles.bundle_id"), nullable=False, index=True),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_table(
        "conversation_members",
        sa.Column("member_id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.conversation_id"), nullable=False, index=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id"), nullable=False, index=True),
        sa.Column("joined_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("conversation_id", "user_id", name="uq_conv_member"),
    )
    op.create_table(
        "group_messages",
        sa.Column("message_id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.conversation_id"), nullable=False, index=True),
        sa.Column("sender_id", sa.String(36), sa.ForeignKey("users.user_id"), nullable=False, index=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_table(
        "group_message_reads",
        sa.Column("read_id", sa.String(36), primary_key=True),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("group_messages.message_id"), nullable=False, index=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id"), nullable=False, index=True),
        sa.Column("read_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("message_id", "user_id", name="uq_msg_read"),
    )


def downgrade() -> None:
    op.drop_table("group_message_reads")
    op.drop_table("group_messages")
    op.drop_table("conversation_members")
    op.drop_table("conversations")
