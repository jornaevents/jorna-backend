"""add date_end to bookings for multi-day events

Revision ID: 0012_add_date_end
Revises: 0011_add_instagram
Create Date: 2026-05-31
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_add_date_end"
down_revision = "0011_add_instagram"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("date_end", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "date_end")
