"""contract change proposals: each sent version, and the client's suggested edits

Revision ID: 0068_contract_proposals
Revises: 0067_contract_documents
Create Date: 2026-10-02

The client–vendor lifecycle plan (docs/DECISIONS.md #23): while a contract
is unsigned, the client can propose edits to it, and the vendor accepts,
declines or revises.

- contract_revisions — each version of a contract's terms, keyed by the
  booking's revision number, so either side can see what changed between
  two versions.
- contract_proposals — a client's suggested version: the revision it was
  based on, the whole proposed terms, their message, and how it ended
  (open | accepted | declined | revised | superseded | withdrawn).

Additive only.
"""
from alembic import op
import sqlalchemy as sa

revision = "0068_contract_proposals"
down_revision = "0067_contract_documents"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "contract_revisions",
        sa.Column("revision_id", sa.String(36), primary_key=True),
        sa.Column(
            "booking_id", sa.String(36),
            sa.ForeignKey("bookings.booking_id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("terms", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("booking_id", "revision", name="uq_contract_revisions_booking_revision"),
    )
    op.create_index("ix_contract_revisions_booking_id", "contract_revisions", ["booking_id"])
    op.create_table(
        "contract_proposals",
        sa.Column("proposal_id", sa.String(36), primary_key=True),
        sa.Column(
            "booking_id", sa.String(36),
            sa.ForeignKey("bookings.booking_id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("proposed", sa.JSON(), nullable=False),
        sa.Column("message", sa.String(2000), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("response_note", sa.String(1000), nullable=True),
        sa.Column("result_revision", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("responded_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_contract_proposals_booking_id", "contract_proposals", ["booking_id"])


def downgrade():
    op.drop_index("ix_contract_proposals_booking_id", table_name="contract_proposals")
    op.drop_table("contract_proposals")
    op.drop_index("ix_contract_revisions_booking_id", table_name="contract_revisions")
    op.drop_table("contract_revisions")
