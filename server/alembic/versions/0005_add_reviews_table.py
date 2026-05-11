"""add reviews table

Revision ID: 0005_add_reviews
Revises: 0004_add_events
Create Date: 2026-05-11
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_add_reviews"
down_revision = "0004_add_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reviews",
        sa.Column("review_id", sa.String(36), primary_key=True),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.booking_id"), nullable=False, index=True),
        sa.Column("vendor_id", sa.String(36), sa.ForeignKey("vendors.vendor_id"), nullable=False, index=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id"), nullable=False, index=True),
        sa.Column("rating", sa.Float, nullable=False),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("booking_id", name="uq_review_booking"),
    )


def downgrade() -> None:
    op.drop_table("reviews")
