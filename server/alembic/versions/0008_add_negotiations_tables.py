"""add negotiations tables

Revision ID: 0008_add_negotiations
Revises: 0007_add_messages
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_add_negotiations"
down_revision = "0007_add_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "negotiations",
        sa.Column("negotiation_id", sa.String(36), primary_key=True),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.booking_id"), nullable=False, unique=True, index=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("current_offer_cents", sa.Integer, nullable=False),
        sa.Column("proposed_by", sa.String(36), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_table(
        "negotiation_offers",
        sa.Column("offer_id", sa.String(36), primary_key=True),
        sa.Column("negotiation_id", sa.String(36), sa.ForeignKey("negotiations.negotiation_id"), nullable=False, index=True),
        sa.Column("proposed_by", sa.String(36), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("amount_cents", sa.Integer, nullable=True),
        sa.Column("message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("negotiation_offers")
    op.drop_table("negotiations")
