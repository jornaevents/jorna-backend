"""An event can have a gift registry

Revision ID: 0089_registry
Revises: 0088_saved_vendors
Create Date: 2026-11-14

- registry_funds: what an event is collecting toward (a honeymoon, a first
  home), with an optional goal.
- registry_gifts: a gift the host recorded against a fund, and whether they've
  thanked the giver. Tracking only — no money moves through Jorna.
"""
import sqlalchemy as sa

from alembic import op

revision = "0089_registry"
down_revision = "0088_saved_vendors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "registry_funds",
        sa.Column("fund_id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("events.event_id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("goal_cents", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_registry_funds_event_id", "registry_funds", ["event_id"])
    op.create_table(
        "registry_gifts",
        sa.Column("gift_id", sa.String(36), primary_key=True),
        sa.Column("fund_id", sa.String(36), sa.ForeignKey("registry_funds.fund_id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_name", sa.String(120), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("thanked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_registry_gifts_fund_id", "registry_gifts", ["fund_id"])


def downgrade() -> None:
    op.drop_index("ix_registry_gifts_fund_id", table_name="registry_gifts")
    op.drop_table("registry_gifts")
    op.drop_index("ix_registry_funds_event_id", table_name="registry_funds")
    op.drop_table("registry_funds")
