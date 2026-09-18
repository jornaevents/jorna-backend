"""timestamps for the deposit's own two-sided "I paid" / "I received it"

Revision ID: 0060_booking_deposit_attestation
Revises: 0059_booking_contract_fields
Create Date: 2026-09-19

A second self-attestation pair alongside manual_payment_marked_at/
manual_payment_confirmed_at (added in 0055) — that existing pair keeps
meaning "the full/remaining balance"; these two mean "the deposit
specifically," for a contract booking with deposit_percent set (0059). A
booking with no deposit configured never touches this pair and behaves
exactly as before this migration.

Additive only: 2 new nullable DateTime columns, no backfill, no new table.
"""
from alembic import op
import sqlalchemy as sa

revision = "0060_booking_deposit_attestation"
down_revision = "0059_booking_contract_fields"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "bookings", sa.Column("deposit_marked_paid_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "bookings", sa.Column("deposit_confirmed_received_at", sa.DateTime(), nullable=True)
    )


def downgrade():
    op.drop_column("bookings", "deposit_confirmed_received_at")
    op.drop_column("bookings", "deposit_marked_paid_at")
