"""let a vendor opt out of Stripe escrow for a manual Venmo/Zelle track

Revision ID: 0054_vendor_payment_method
Revises: 0053_booking_cancellation_split
Create Date: 2026-09-07

First phase of "optional escrow": a vendor can choose between the existing
Stripe card-charge/hold flow ("stripe") and being paid directly via Venmo or
Zelle outside the app ("manual"). This migration only adds the columns —
nothing yet reads payment_method to change booking behavior.

vendors.payment_method defaults to "stripe" (and is backfilled to it via
server_default, since the column is NOT NULL) so every existing vendor's
behavior is unchanged until they opt in. venmo_handle/zelle_contact are
plain nullable text, only meaningful once a vendor picks "manual".

bookings.payment_method has no default and is left NULL for every existing
row — it's a per-booking snapshot of the vendor's setting taken at booking
creation, going forward only, not something to backfill onto old bookings
that predate this feature entirely.

Additive only: 4 new nullable-or-defaulted columns, no backfill, no new
table.
"""
from alembic import op
import sqlalchemy as sa

revision = "0054_vendor_payment_method"
down_revision = "0053_booking_cancellation_split"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "vendors",
        sa.Column(
            "payment_method",
            sa.String(length=20),
            nullable=False,
            server_default="stripe",
        ),
    )
    op.add_column("vendors", sa.Column("venmo_handle", sa.String(length=255), nullable=True))
    op.add_column("vendors", sa.Column("zelle_contact", sa.String(length=255), nullable=True))
    op.add_column("bookings", sa.Column("payment_method", sa.String(length=20), nullable=True))


def downgrade():
    op.drop_column("bookings", "payment_method")
    op.drop_column("vendors", "zelle_contact")
    op.drop_column("vendors", "venmo_handle")
    op.drop_column("vendors", "payment_method")
