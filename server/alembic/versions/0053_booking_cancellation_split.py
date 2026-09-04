"""record the client/vendor split when a paid booking is cancelled

Revision ID: 0053_booking_cancellation_split
Revises: 0052_merge_heads
Create Date: 2026-09-04

Backs the new cancellation policy (stripe_service.cancel_booking /
cancellation_split): a client cancelling within 24h of the vendor's
acceptance gets a full refund; after that, up to the day before the event,
nothing goes back to the client and the payment splits between the platform
and the vendor on a linear ramp instead.

refund_cents and vendor_cancellation_cents are kept distinct from the
existing platform_fee_cents (the ordinary transaction fee, unrelated to a
cancellation) so a cancelled booking's money can be accounted for
accurately rather than folded into the flat 100%-back assumption the
'refunded' payment_status otherwise implies.

Additive only: 3 new nullable columns, no backfill, no new table.
"""
from alembic import op
import sqlalchemy as sa

revision = "0053_booking_cancellation_split"
down_revision = "0052_merge_heads"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bookings", sa.Column("cancelled_at", sa.DateTime(), nullable=True))
    op.add_column("bookings", sa.Column("refund_cents", sa.Integer(), nullable=True))
    op.add_column(
        "bookings", sa.Column("vendor_cancellation_cents", sa.Integer(), nullable=True)
    )


def downgrade():
    op.drop_column("bookings", "vendor_cancellation_cents")
    op.drop_column("bookings", "refund_cents")
    op.drop_column("bookings", "cancelled_at")
