"""Events can run for more than one day

Revision ID: 0087_event_date_end
Revises: 0086_service_vibe
Create Date: 2026-11-14

- events.date_end: the last day of a celebration that runs across several
  (a wedding weekend); null for a one-day event.
"""
import sqlalchemy as sa

from alembic import op

revision = "0087_event_date_end"
down_revision = "0086_service_vibe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("date_end", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("events", "date_end")
