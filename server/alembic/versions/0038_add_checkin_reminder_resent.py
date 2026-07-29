"""a client's own nudge, kept apart from the automatic one

Revision ID: 0038_checkin_resend
Revises: 0037_checkin_reminder
Create Date: 2026-07-29

A client can resend a vendor their check-in email. That needs its own timestamp
rather than reusing checkin_reminder_sent_at, and the reason is a trap: the
automatic sweep sends only where checkin_reminder_sent_at is null. Write a
manual resend into that column and a client who nudges a vendor early has
silently switched off the real half-hour reminder — by doing them a favour.

Two columns, two meanings. One records what the server did on schedule; this
one records what the client asked for, and is what the cooldown reads.
"""
from alembic import op
import sqlalchemy as sa

revision = "0038_checkin_resend"
down_revision = "0037_checkin_reminder"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "bookings",
        sa.Column("checkin_reminder_resent_at", sa.DateTime, nullable=True),
    )


def downgrade():
    op.drop_column("bookings", "checkin_reminder_resent_at")
