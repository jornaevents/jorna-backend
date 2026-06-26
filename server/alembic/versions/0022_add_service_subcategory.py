"""add subcategory to services

Revision ID: 0022_service_subcategory
Revises: 0021_password_reset
Create Date: 2026-06-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0022_service_subcategory"
down_revision = "0021_password_reset"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "services",
        sa.Column("subcategory", sa.String(50), nullable=True),
    )


def downgrade():
    op.drop_column("services", "subcategory")
