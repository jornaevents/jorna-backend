"""add open_to_long_distance to vendors

Revision ID: 0013_add_long_distance
Revises: 0012_add_date_end
Create Date: 2026-05-31
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_add_long_distance"
down_revision = "0012_add_date_end"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vendors",
        sa.Column("open_to_long_distance", sa.Boolean, nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("vendors", "open_to_long_distance")
