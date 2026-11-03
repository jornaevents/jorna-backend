"""Clients on the contract link can message their vendor (DECISIONS #41)

Revision ID: 0083_guest_messages
Revises: 0082_business_name_terms
Create Date: 2026-11-03

A client who only has the contract link — no account — writes into the
booking's ordinary thread, which the vendor reads in Messages. Their messages
have no user to point at, so group_messages.sender_id becomes nullable (null =
the client on the link). Two booking columns track that client's side of the
thread: when they last read it, and when they were last emailed a reply.
"""
import sqlalchemy as sa
from alembic import op

revision = "0083_guest_messages"
down_revision = "0082_business_name_terms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("group_messages", "sender_id", existing_type=sa.String(length=36), nullable=True)
    op.add_column("bookings", sa.Column("guest_messages_read_at", sa.DateTime(), nullable=True))
    op.add_column("bookings", sa.Column("guest_message_emailed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "guest_message_emailed_at")
    op.drop_column("bookings", "guest_messages_read_at")
    # Messages from link clients have no sender to restore.
    op.execute("DELETE FROM group_message_reads WHERE message_id IN (SELECT message_id FROM group_messages WHERE sender_id IS NULL)")
    op.execute("DELETE FROM group_messages WHERE sender_id IS NULL")
    op.alter_column("group_messages", "sender_id", existing_type=sa.String(length=36), nullable=False)
