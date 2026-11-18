"""Start and end times on a lead, from the inquiry form

Revision ID: 0094_lead_times
Revises: 0093_usage_events
Create Date: 2026-11-18

- leads.time_start, leads.time_end: optional "HH:MM" from the inquiry form
  (DECISIONS #58), so the contract can fill its times from the client's own
  answer instead of only from chat. NULL when not given.
"""
import sqlalchemy as sa

from alembic import op

revision = "0094_lead_times"
down_revision = "0093_usage_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("time_start", sa.String(5), nullable=True))
    op.add_column("leads", sa.Column("time_end", sa.String(5), nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "time_end")
    op.drop_column("leads", "time_start")
