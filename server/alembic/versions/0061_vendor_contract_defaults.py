"""vendor-level contract defaults

Revision ID: 0061_vendor_contract_defaults
Revises: 0060_booking_deposit_attestation
Create Date: 2026-09-19

Seed values for the Contracts builder's "start from what I usually offer"
step — a vendor sets these once instead of typing the same deposit %/
cancellation window/overtime rate into every new booking. Pure defaults:
no backend logic reads these beyond returning them on GET /vendors/me: a
booking snapshots its own copy at creation time (see
Booking.deposit_percent etc., 0059) and is never recomputed from a later
change to these.

Additive only: 6 new nullable columns, no backfill, no new table.
"""
from alembic import op
import sqlalchemy as sa

revision = "0061_vendor_contract_defaults"
down_revision = "0060_booking_deposit_attestation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("vendors", sa.Column("default_deposit_percent", sa.Integer(), nullable=True))
    op.add_column(
        "vendors", sa.Column("default_cancellation_window_hours", sa.Integer(), nullable=True)
    )
    op.add_column(
        "vendors", sa.Column("default_overtime_rate_cents", sa.Integer(), nullable=True)
    )
    op.add_column("vendors", sa.Column("default_addon_rate_cents", sa.Integer(), nullable=True))
    op.add_column("vendors", sa.Column("default_contract_terms", sa.JSON(), nullable=True))
    op.add_column("vendors", sa.Column("default_guest_count_mode", sa.String(20), nullable=True))


def downgrade():
    op.drop_column("vendors", "default_guest_count_mode")
    op.drop_column("vendors", "default_contract_terms")
    op.drop_column("vendors", "default_addon_rate_cents")
    op.drop_column("vendors", "default_overtime_rate_cents")
    op.drop_column("vendors", "default_cancellation_window_hours")
    op.drop_column("vendors", "default_deposit_percent")
