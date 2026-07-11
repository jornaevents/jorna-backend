"""add checkout_session_id to bookings

Revision ID: 0028_booking_checkout_session_id
Revises: 0027_service_venue_location
Create Date: 2026-07-11

Stores the Stripe Checkout Session id on the booking so the app can reconcile a
booking's payment status directly with Stripe when the customer returns from
hosted Checkout — a safety net for a delayed or misconfigured
payment_intent.succeeded webhook (see stripe_service.sync_booking_payment).
Nullable/additive: existing rows stay null.
"""
from alembic import op
import sqlalchemy as sa

revision = "0028_booking_checkout_session_id"
down_revision = "0027_service_venue_location"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bookings", sa.Column("checkout_session_id", sa.String(length=255), nullable=True))


def downgrade():
    op.drop_column("bookings", "checkout_session_id")
