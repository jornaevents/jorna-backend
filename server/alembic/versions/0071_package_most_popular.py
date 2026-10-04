"""services.is_popular: the package a vendor marks "Most popular"

Revision ID: 0071_package_most_popular
Revises: 0070_default_payment_plan
Create Date: 2026-10-03

At most one per vendor (service_service keeps it that way: marking one
clears the rest). Shown as a badge on the vendor's public listing.

Additive only.
"""
from alembic import op
import sqlalchemy as sa

revision = "0071_package_most_popular"
down_revision = "0070_default_payment_plan"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "services",
        sa.Column("is_popular", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("services", "is_popular")
