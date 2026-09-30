"""guest/contract bookings: nullable user_id, guest contact, link token

Revision ID: 0058_booking_guest_fields
Revises: 0057_booking_rejected_reason
Create Date: 2026-09-19

Phase 1 of the vendor-authored "Contracts" flow: a vendor can create a
booking for a client who has never used Jorna and never will log in — the
client fills in their own contact/venue details and e-signs via a public
link with no account at all (see docs/DECISIONS.md's guest-booking entry).
That means bookings.user_id can no longer be NOT NULL, and a guest booking
needs somewhere to keep the contact info a User row would otherwise supply,
plus the link's own credential.

Relaxing user_id's NOT NULL is the one non-purely-additive change in this
migration (existing rows are untouched — dropping a NOT NULL constraint
never rewrites data, unlike adding one). Every code path that reads
Booking.user_id for a client-side comparison/join needs a companion guard
for the null case before any endpoint that can actually create a null-user_id
row is exposed — see the guard-audit commit landing alongside this one.
"""
from alembic import op
import sqlalchemy as sa

revision = "0058_booking_guest_fields"
down_revision = "0057_booking_rejected_reason"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("bookings", "user_id", existing_type=sa.String(36), nullable=True)
    op.add_column("bookings", sa.Column("guest_name", sa.String(255), nullable=True))
    op.add_column("bookings", sa.Column("guest_email", sa.String(255), nullable=True))
    op.add_column("bookings", sa.Column("guest_phone", sa.String(50), nullable=True))
    op.add_column("bookings", sa.Column("contract_token", sa.String(64), nullable=True))
    op.create_index(
        "ix_bookings_contract_token", "bookings", ["contract_token"], unique=True
    )


def downgrade():
    op.drop_index("ix_bookings_contract_token", table_name="bookings")
    op.drop_column("bookings", "contract_token")
    op.drop_column("bookings", "guest_phone")
    op.drop_column("bookings", "guest_email")
    op.drop_column("bookings", "guest_name")
    op.alter_column("bookings", "user_id", existing_type=sa.String(36), nullable=False)
