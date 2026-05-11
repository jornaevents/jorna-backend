"""add is_admin to users

Revision ID: 0006_add_is_admin
Revises: 0005_add_reviews
Create Date: 2026-05-11
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_add_is_admin"
down_revision = "0005_add_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_admin", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("users", "is_admin")
