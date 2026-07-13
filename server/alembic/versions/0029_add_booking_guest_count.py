"""add guest_count to bookings

Revision ID: 0029_booking_guest_count
Revises: 0028_booking_checkout_session_id
Create Date: 2026-07-13

Persists the event's guest count on the booking so a per-person service's total
(rate x guests) can be recomputed and audited at checkout — instead of silently
charging the bare per-person rate when the quantity is otherwise unknown.
Nullable/additive: existing rows stay null.
"""
from alembic import op
import sqlalchemy as sa

revision = "0029_booking_guest_count"
down_revision = "0028_booking_checkout_session_id"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bookings", sa.Column("guest_count", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("bookings", "guest_count")
