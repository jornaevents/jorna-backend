"""add the client's own address pin to events

Revision ID: 0033_event_address_coords
Revises: 0032_password_nullable
Create Date: 2026-07-28

An event's venue_latitude/venue_longitude are derived: sync_event_venue copies
them from the live venue-category booking and clears them when there isn't one,
so a venue that's been removed can't leave the other vendors checking in against
it. That is the right behaviour for a pin the system owns.

It leaves nowhere to put a pin the client owns. An event held somewhere the
client arranged themselves — the family hall, a garden, a venue booked outside
Jorna — has a full address and no coordinates at all, so nobody can check in and
the vendors' payouts wait on a confirmation nobody can give.

These two columns are that address, geocoded. Nothing derived ever writes them,
so the sync above can go on clearing what it owns without touching what it
doesn't. Nullable and additive: existing events stay null until their address is
saved again.
"""
from alembic import op
import sqlalchemy as sa

revision = "0033_event_address_coords"
down_revision = "0032_password_nullable"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("events", sa.Column("address_latitude", sa.Float(), nullable=True))
    op.add_column("events", sa.Column("address_longitude", sa.Float(), nullable=True))


def downgrade():
    op.drop_column("events", "address_longitude")
    op.drop_column("events", "address_latitude")
