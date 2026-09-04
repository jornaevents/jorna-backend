"""backfill bundle_id onto existing booking-thread conversations

Revision ID: 0047_booking_thread_bundle_id
Revises: 0046_service_require_counts
Create Date: 2026-09-03

conversation_service._create_thread hardcoded bundle_id=None for every
conversation it made, including booking threads — even though the booking a
thread is about always belongs to a bundle. Fixed going forward in that same
function (now passes booking.bundle_id through), and open_booking_thread
self-heals a stale thread's bundle_id the next time anyone opens it. This is
the one-time catch-up for threads nobody happens to reopen, so the frontend's
"Answer on the booking" link can deep-link every existing thread too, not
just new ones.

Data-only: no schema change, no column added. Fully idempotent — only ever
moves a row from NULL to a value that was already true, so safe to re-run.
"""
from alembic import op

revision = "0047_booking_thread_bundle_id"
down_revision = "0046_service_require_counts"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        UPDATE conversations
        SET bundle_id = (
            SELECT b.bundle_id FROM bookings b WHERE b.booking_id = conversations.booking_id
        )
        WHERE subject_type = 'booking'
          AND bundle_id IS NULL
          AND EXISTS (
              SELECT 1 FROM bookings b
              WHERE b.booking_id = conversations.booking_id
                AND b.bundle_id IS NOT NULL
          )
        """
    )


def downgrade():
    # Backfilled data can't be distinguished from a value set at creation
    # time afterward, so this migration is not reversible. No-op.
    pass
