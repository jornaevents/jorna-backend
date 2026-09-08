"""timestamps for the manual track's two-sided "I paid" / "I received it"

Revision ID: 0055_manual_payment_attestation
Revises: 0054_vendor_payment_method
Create Date: 2026-09-08

Second phase of "optional escrow": a manual-track booking never gets an
automatic Stripe charge, so there's nothing for Jorna to verify — instead
the client marks a booking paid and the vendor confirms receiving it
(stripe_service.mark_booking_paid / confirm_payment_received). These two
timestamps are the audit trail for that, same convention as paid_at /
funds_released_at / confirmed_at above them on this table.

Additive only: 2 new nullable DateTime columns, no backfill, no new table.
"""
from alembic import op
import sqlalchemy as sa

revision = "0055_manual_payment_attestation"
down_revision = "0054_vendor_payment_method"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "bookings", sa.Column("manual_payment_marked_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "bookings", sa.Column("manual_payment_confirmed_at", sa.DateTime(), nullable=True)
    )


def downgrade():
    op.drop_column("bookings", "manual_payment_confirmed_at")
    op.drop_column("bookings", "manual_payment_marked_at")
