"""remember where each user's message-digest sweep left off

Revision ID: 0048_message_digest_at
Revises: 0047_booking_thread_bundle_id
Create Date: 2026-09-03

The digest sweep runs periodically and emails a user a summary of messages
they haven't read since their last digest. A message should land in exactly
one digest — the next one after it arrives — not every sweep pass until it's
read, so the sweep needs to know per user what "since last time" means.

A timestamp rather than a flag, same reasoning as checkin_reminder_sent_at
(0037): when someone's last digest went out is the first thing worth knowing
if they say they never got one.
"""
from alembic import op
import sqlalchemy as sa

revision = "0048_message_digest_at"
down_revision = "0047_booking_thread_bundle_id"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("last_message_digest_at", sa.DateTime, nullable=True),
    )


def downgrade():
    op.drop_column("users", "last_message_digest_at")
