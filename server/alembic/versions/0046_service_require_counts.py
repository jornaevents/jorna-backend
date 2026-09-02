"""add opt-in required-guest/performer-count flags to services

Revision ID: 0046_service_require_counts
Revises: 0045_performer_pricing
Create Date: 2026-09-02

booking_gaps() already demands a guest count on a per-person-priced service
and a performer count on a per-performer-priced one. This adds two opt-in
flags a vendor can set independently of price_unit — a flat-rate caterer who
still wants a headcount before deciding whether to accept, say. Additive
only: booking_gaps() ORs these onto the existing price-unit-driven
requirement, never loosens it. Existing services get False for both, so
nothing becomes newly required until a vendor explicitly opts in.
"""
from alembic import op
import sqlalchemy as sa

revision = "0046_service_require_counts"
down_revision = "0045_performer_pricing"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "services",
        sa.Column("require_guest_count", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "services",
        sa.Column("require_performer_count", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("services", "require_performer_count")
    op.drop_column("services", "require_guest_count")
