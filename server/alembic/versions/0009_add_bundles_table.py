"""add bundles table and bundle_id to bookings

Revision ID: 0009_add_bundles
Revises: 0008_add_negotiations
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_add_bundles"
down_revision = "0008_add_negotiations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bundles",
        sa.Column("bundle_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id"), nullable=False, index=True),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("events.event_id"), nullable=True, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.add_column("bookings", sa.Column("bundle_id", sa.String(36), sa.ForeignKey("bundles.bundle_id"), nullable=True, index=True))


def downgrade() -> None:
    op.drop_column("bookings", "bundle_id")
    op.drop_table("bundles")
