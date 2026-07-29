"""remember which vendors have had their half-hour warning

Revision ID: 0037_checkin_reminder
Revises: 0036_guests
Create Date: 2026-07-29

The reminder sweep runs every few minutes and looks for bookings starting in
about half an hour. "About" is a window, and a window a sweep passes through
several times would send the same email several times — so the send is recorded
and the window filters on it.

A timestamp rather than a flag, because when it went out is the first thing
anybody asks when a vendor says they never got it.
"""
from alembic import op
import sqlalchemy as sa

revision = "0037_checkin_reminder"
down_revision = "0036_guests"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "bookings",
        sa.Column("checkin_reminder_sent_at", sa.DateTime, nullable=True),
    )


def downgrade():
    op.drop_column("bookings", "checkin_reminder_sent_at")
