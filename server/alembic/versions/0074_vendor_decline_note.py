"""A vendor's note when declining a request (docs/DECISIONS.md #28)

Revision ID: 0074_vendor_decline_note
Revises: 0073_signing_codes
Create Date: 2026-10-14

- bookings.vendor_decline_note: the optional reason a vendor gives when
  turning a marketplace request down ("Booked that weekend"), shown to the
  client on their plan and in the email.

Additive only.
"""
from alembic import op
import sqlalchemy as sa

revision = "0074_vendor_decline_note"
down_revision = "0073_signing_codes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bookings", sa.Column("vendor_decline_note", sa.String(500), nullable=True))


def downgrade():
    op.drop_column("bookings", "vendor_decline_note")
