"""Clients can save vendors

Revision ID: 0088_saved_vendors
Revises: 0087_event_date_end
Create Date: 2026-11-14

- saved_vendors: the vendors a client has hearted in Explore, to compare side
  by side. One row per client and vendor.
"""
import sqlalchemy as sa

from alembic import op

revision = "0088_saved_vendors"
down_revision = "0087_event_date_end"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_vendors",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("vendor_id", sa.String(36), sa.ForeignKey("vendors.vendor_id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "vendor_id", name="uq_saved_vendors_user_vendor"),
    )
    op.create_index("ix_saved_vendors_user_id", "saved_vendors", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_saved_vendors_user_id", table_name="saved_vendors")
    op.drop_table("saved_vendors")
