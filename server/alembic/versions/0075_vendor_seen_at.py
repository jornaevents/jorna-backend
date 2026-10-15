"""When the vendor first opened a signed booking (docs/DECISIONS.md #30)

Revision ID: 0075_vendor_seen_at
Revises: 0074_vendor_decline_note
Create Date: 2026-10-15

- bookings.vendor_seen_at: set the first time the vendor opens a booking on
  their Bookings page. A signed booking without it is "new", and counts on
  the Bookings tab's badge.

Bookings already signed are marked seen as of their signature, so the badge
doesn't light up with every booking a vendor has ever had.
"""
from alembic import op
import sqlalchemy as sa

revision = "0075_vendor_seen_at"
down_revision = "0074_vendor_decline_note"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bookings", sa.Column("vendor_seen_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE bookings SET vendor_seen_at = signed_at WHERE signed_at IS NOT NULL")


def downgrade():
    op.drop_column("bookings", "vendor_seen_at")
