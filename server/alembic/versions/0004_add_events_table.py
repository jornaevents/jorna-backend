"""add events table

Revision ID: 0004_add_events
Revises: 0003_google_signup
Create Date: 2026-05-03
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_add_events"
down_revision = "0003_google_signup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id"), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("date_iso", sa.String(50), nullable=False),
        sa.Column("location", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("guest_count", sa.Integer, nullable=True),
        sa.Column("budget", sa.Float, nullable=True),
        sa.Column("services_needed", sa.JSON, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("events")
