"""track each booking's own Google Calendar event, for write-back

Revision ID: 0050_booking_google_event_id
Revises: 0049_google_granted_scopes
Create Date: 2026-09-03

Booking write-back (calendar_service.sync_booking_to_calendar /
remove_booking_from_calendar) needs somewhere to remember which Google
event a booking became, so a later update or delete targets the same
event rather than creating a duplicate every time. Null until a booking is
approved and a vendor with write access is connected; cleared again once
the booking ends.

Data-only in effect: no schema change beyond the new nullable column, no
backfill — every existing booking correctly has no Google event yet, since
write-back didn't exist before this.
"""
from alembic import op
import sqlalchemy as sa

revision = "0050_booking_google_event_id"
down_revision = "0049_google_granted_scopes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "bookings",
        sa.Column("google_event_id", sa.String(255), nullable=True),
    )


def downgrade():
    op.drop_column("bookings", "google_event_id")
