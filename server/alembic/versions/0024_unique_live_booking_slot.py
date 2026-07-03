"""partial unique index: one live booking per bundle/vendor/service/date

Revision ID: 0024_unique_live_booking
Revises: 0023_reports_blocks
Create Date: 2026-07-03
"""
from alembic import op
import sqlalchemy as sa

revision = "0024_unique_live_booking"
down_revision = "0023_reports_blocks"
branch_labels = None
depends_on = None

# Partial: rejected/cancelled rows don't block a legitimate re-book of the
# same slot. NULL bundle_ids are exempt by SQL NULL semantics.
_WHERE = sa.text("status NOT IN ('rejected', 'cancelled')")


def upgrade():
    op.create_index(
        "uq_live_booking_slot",
        "bookings",
        ["bundle_id", "vendor_id", "service_id", "date_iso"],
        unique=True,
        postgresql_where=_WHERE,
        sqlite_where=_WHERE,
    )


def downgrade():
    op.drop_index("uq_live_booking_slot", table_name="bookings")
