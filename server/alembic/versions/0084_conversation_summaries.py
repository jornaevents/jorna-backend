"""AI summary lines for the Messages inbox (DECISIONS #43)

Revision ID: 0084_conversation_summaries
Revises: 0083_guest_messages
Create Date: 2026-11-04

conversation_summaries holds one line per thread, written by the summary
sweep and read by GET /conversations. It has no foreign keys on purpose: see
the ConversationSummary docstring. users.ai_summaries_enabled is the "Show AI
summaries" setting. group_messages.created_at gets an index, because the sweep
asks "which threads had a message in the last day" every 30 seconds.

Nothing here changes behaviour on its own: summaries stay off until
SUMMARIES_ENABLED is set.
"""
import sqlalchemy as sa
from alembic import op

revision = "0084_conversation_summaries"
down_revision = "0083_guest_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_summaries",
        sa.Column("conversation_id", sa.String(length=36), primary_key=True),
        sa.Column("text", sa.String(length=200), nullable=True),
        sa.Column("to_message_id", sa.String(length=36), nullable=True),
        sa.Column("to_message_at", sa.DateTime(), nullable=True),
        sa.Column("sender_ids", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ok"),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("runs_on", sa.String(length=10), nullable=True),
        sa.Column("runs_today", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("force", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "users",
        sa.Column("ai_summaries_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_group_messages_created_at", "group_messages", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_group_messages_created_at", table_name="group_messages")
    op.drop_column("users", "ai_summaries_enabled")
    op.drop_table("conversation_summaries")
