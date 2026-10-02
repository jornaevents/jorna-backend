"""contract negotiation drafts: each side's unsent changes, saved on the server

Revision ID: 0069_contract_drafts
Revises: 0068_contract_proposals
Create Date: 2026-10-02

The negotiation workspace (docs/DECISIONS.md #24) has a Save draft button
for both sides: the client's proposal before they send it, and the
vendor's revised values before they send their answer. One draft per
contract per side, kept until that side sends something.

Additive only.
"""
from alembic import op
import sqlalchemy as sa

revision = "0069_contract_drafts"
down_revision = "0068_contract_proposals"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "contract_drafts",
        sa.Column("draft_id", sa.String(36), primary_key=True),
        sa.Column(
            "booking_id", sa.String(36),
            sa.ForeignKey("bookings.booking_id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("party", sa.String(10), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("message", sa.String(2000), nullable=True),
        sa.Column("proposal_id", sa.String(36), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("booking_id", "party", name="uq_contract_drafts_booking_party"),
    )
    op.create_index("ix_contract_drafts_booking_id", "contract_drafts", ["booking_id"])


def downgrade():
    op.drop_index("ix_contract_drafts_booking_id", table_name="contract_drafts")
    op.drop_table("contract_drafts")
