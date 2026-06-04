"""add negotiation preference flags to users and vendors

Revision ID: 0014_negotiation_flags
Revises: 0013_add_long_distance
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_negotiation_flags"
down_revision = "0013_add_long_distance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Vendor: willing to negotiate price / location
    op.add_column("vendors", sa.Column("open_to_price_negotiation", sa.Boolean, nullable=False, server_default="false"))
    op.add_column("vendors", sa.Column("open_to_location_negotiation", sa.Boolean, nullable=False, server_default="false"))
    # User (client): willing to negotiate price / flexible on location
    op.add_column("users", sa.Column("open_to_price_negotiation", sa.Boolean, nullable=False, server_default="false"))
    op.add_column("users", sa.Column("flexible_on_location", sa.Boolean, nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("vendors", "open_to_price_negotiation")
    op.drop_column("vendors", "open_to_location_negotiation")
    op.drop_column("users", "open_to_price_negotiation")
    op.drop_column("users", "flexible_on_location")
