"""add negotiable flag to services

Revision ID: 0026_service_negotiable
Revises: 0025_service_category_backfill
Create Date: 2026-07-09

Negotiation moves from a vendor-wide setting to a per-service toggle the vendor
sets when creating/editing a service. Adds services.negotiable (NOT NULL,
default False) — existing services get False, so nothing becomes negotiable
until a vendor explicitly opts a service in.
"""
from alembic import op
import sqlalchemy as sa

revision = "0026_service_negotiable"
down_revision = "0025_service_category_backfill"
branch_labels = None
depends_on = None


def upgrade():
    # server_default=false backfills existing rows; the ORM (default=False) also
    # sets it on new inserts, so the column is always populated.
    op.add_column(
        "services",
        sa.Column("negotiable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("services", "negotiable")
