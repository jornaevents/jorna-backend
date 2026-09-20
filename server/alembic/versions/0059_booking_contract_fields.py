"""contract terms + e-signature fields on bookings

Revision ID: 0059_booking_contract_fields
Revises: 0058_booking_guest_fields
Create Date: 2026-09-19

Phase 2 of the vendor-authored "Contracts" flow: the terms a vendor sets
when creating a booking/contract (deposit, cancellation window, overtime
and day-of addon rates, plus free-form clauses like equipment/travel), and
the client's e-signature. Typed columns for anything ever compared or
computed against (deposit math, date-math sentences); free-form prose terms
live in one JSON blob (contract_terms) instead of a column per clause, so a
new clause wording doesn't need its own migration.

Additive only: 8 new nullable columns, no backfill, no new table.
"""
from alembic import op
import sqlalchemy as sa

revision = "0059_booking_contract_fields"
down_revision = "0058_booking_guest_fields"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bookings", sa.Column("deposit_percent", sa.Integer(), nullable=True))
    op.add_column("bookings", sa.Column("deposit_amount_cents", sa.Integer(), nullable=True))
    op.add_column("bookings", sa.Column("cancellation_window_hours", sa.Integer(), nullable=True))
    op.add_column("bookings", sa.Column("overtime_rate_cents", sa.Integer(), nullable=True))
    op.add_column("bookings", sa.Column("addon_rate_cents", sa.Integer(), nullable=True))
    op.add_column("bookings", sa.Column("contract_terms", sa.JSON(), nullable=True))
    op.add_column("bookings", sa.Column("signer_name", sa.String(255), nullable=True))
    op.add_column("bookings", sa.Column("signed_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("bookings", "signed_at")
    op.drop_column("bookings", "signer_name")
    op.drop_column("bookings", "contract_terms")
    op.drop_column("bookings", "addon_rate_cents")
    op.drop_column("bookings", "overtime_rate_cents")
    op.drop_column("bookings", "cancellation_window_hours")
    op.drop_column("bookings", "deposit_amount_cents")
    op.drop_column("bookings", "deposit_percent")
