"""Usage events: how often vendors use what's built for them

Revision ID: 0093_usage_events
Revises: 0092_lead_inquiry_link
Create Date: 2026-11-18

- usage_events: one row per counted action (DECISIONS #57), so version 2 of
  Contract Builder from Chat, and any later AI, is judged on real use. A
  name from an allowed list, who did it, when, and a few short fields.
  Never message text.
"""
import sqlalchemy as sa

from alembic import op

revision = "0093_usage_events"
down_revision = "0092_lead_inquiry_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("props", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_usage_events_name_created", "usage_events", ["name", "created_at"])
    op.create_index("ix_usage_events_user_created", "usage_events", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_usage_events_user_created", table_name="usage_events")
    op.drop_index("ix_usage_events_name_created", table_name="usage_events")
    op.drop_table("usage_events")
