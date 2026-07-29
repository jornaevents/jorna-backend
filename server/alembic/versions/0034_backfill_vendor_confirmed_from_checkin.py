"""a check-in that happened before payment still counts as a confirmation

Revision ID: 0034_backfill_vendor_confirmed
Revises: 0033_event_address_coords
Create Date: 2026-07-28

A vendor's GPS check-in is their confirmation that the event happened, but it
was only recorded as one when the booking was already paid. A vendor who checked
in before the client's payment cleared was marked present and never confirmed,
and nothing revisited it afterwards — so the client confirmed, and then waited
on a second confirmation that had no way of arriving. The money stayed on the
platform with both parties believing they'd done their part.

check_in no longer makes that distinction. This backfills the bookings it
already got wrong: where a vendor checked in and was never confirmed, their
check-in time becomes their confirmation time, which is the truth of it.

Only rows with money still held are touched. A refunded or released booking is
finished, and rewriting its history would change a record rather than repair
one.

vendor_checked_in_at is a String column holding an ISO timestamp and
vendor_confirmed_at is a DateTime, so the copy goes through Python rather than
relying on any one database's idea of a cast.
"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime

revision = "0034_backfill_vendor_confirmed"
down_revision = "0033_event_address_coords"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT booking_id, vendor_checked_in_at FROM bookings "
            "WHERE vendor_checked_in_at IS NOT NULL "
            "AND vendor_confirmed_at IS NULL "
            "AND payment_status = 'paid'"
        )
    ).fetchall()

    for booking_id, checked_in in rows:
        try:
            # Python's fromisoformat won't take a trailing Z before 3.11.
            when = datetime.fromisoformat(str(checked_in).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        conn.execute(
            sa.text(
                "UPDATE bookings SET vendor_confirmed_at = :when "
                "WHERE booking_id = :booking_id"
            ),
            {"when": when, "booking_id": booking_id},
        )


def downgrade():
    # Deliberately not reversible. There's no column recording which
    # confirmations came from this backfill, and clearing every confirmation
    # that has a check-in beside it would discard real ones.
    pass
