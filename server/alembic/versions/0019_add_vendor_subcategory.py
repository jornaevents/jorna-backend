"""add subcategory to vendors

Revision ID: 0019_vendor_subcategory
Revises: 0018_remap_vendor_categories
Create Date: 2026-06-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0019_vendor_subcategory"
down_revision = "0018_remap_vendor_categories"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "vendors",
        sa.Column("subcategory", sa.String(100), nullable=True),
    )
    op.create_index("ix_vendors_subcategory", "vendors", ["subcategory"])


def downgrade():
    op.drop_index("ix_vendors_subcategory", table_name="vendors")
    op.drop_column("vendors", "subcategory")
