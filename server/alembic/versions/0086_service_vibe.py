"""Packages have a vibe (elegant, traditional, modern, fusion, playful, minimal)

Revision ID: 0086_service_vibe
Revises: 0085_lead_location_package
Create Date: 2026-11-13

- services.vibe: the feel of the package, picked by the vendor; null until
  they pick one. The allowed values live in app.routers.services (PACKAGE_VIBES).
"""
import sqlalchemy as sa

from alembic import op

revision = "0086_service_vibe"
down_revision = "0085_lead_location_package"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("services", sa.Column("vibe", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("services", "vibe")
