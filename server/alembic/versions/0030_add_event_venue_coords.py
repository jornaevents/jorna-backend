"""add venue coords to events

Revision ID: 0030_event_venue_coords
Revises: 0029_booking_guest_count
Create Date: 2026-07-14

Makes the Event the source of truth for its venue's GPS coordinates. These are
synced from the live venue-category booking and cleared when no venue is booked,
so a removed/refunded venue can't orphan the other bookings' check-in.
Nullable/additive: existing rows stay null until re-synced.
"""
from alembic import op
import sqlalchemy as sa

revision = "0030_event_venue_coords"
down_revision = "0029_booking_guest_count"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("events", sa.Column("venue_latitude", sa.Float(), nullable=True))
    op.add_column("events", sa.Column("venue_longitude", sa.Float(), nullable=True))


def downgrade():
    op.drop_column("events", "venue_longitude")
    op.drop_column("events", "venue_latitude")
