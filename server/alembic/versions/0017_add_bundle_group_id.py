"""add bundle_group_id to bundles

Revision ID: 0017_bundle_group_id
Revises: 0016_refresh_tokens
Create Date: 2026-06-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_bundle_group_id"
down_revision = "0016_refresh_tokens"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "bundles",
        sa.Column("bundle_group_id", sa.String(36), nullable=True),
    )
    op.create_index("ix_bundles_bundle_group_id", "bundles", ["bundle_group_id"])


def downgrade():
    op.drop_index("ix_bundles_bundle_group_id", table_name="bundles")
    op.drop_column("bundles", "bundle_group_id")
