"""contract proposals: line items, payment schedule, clauses, signed snapshot, templates, timeline

Revision ID: 0065_contract_proposals
Revises: 0064_contract_lifecycle
Create Date: 2026-09-26

Phase 2b of the package/contract overhaul (docs/DECISIONS.md #16):

- bookings.line_items / discount_cents — several packages and add-ons,
  snapshotted; amount_cents stays the grand total.
- bookings.payment_schedule — installments instead of one deposit.
- bookings.terms_clauses — clause text as sent.
- bookings.revision, signed_snapshot, signed_snapshot_sha256 — which version
  was signed, and exactly what it said.
- contract_templates — a vendor's templates, per account instead of per
  browser.
- contract_events — the contract timeline.

Additive only, no backfill: a contract from before this keeps its single
deposit and legacy terms, and its timeline is derived from timestamps it
already has.
"""
from alembic import op
import sqlalchemy as sa

revision = "0065_contract_proposals"
down_revision = "0064_contract_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bookings", sa.Column("line_items", sa.JSON(), nullable=True))
    op.add_column("bookings", sa.Column("discount_cents", sa.Integer(), nullable=True))
    op.add_column("bookings", sa.Column("payment_schedule", sa.JSON(), nullable=True))
    op.add_column("bookings", sa.Column("terms_clauses", sa.JSON(), nullable=True))
    op.add_column("bookings", sa.Column("revision", sa.Integer(), nullable=True))
    op.add_column("bookings", sa.Column("signed_snapshot", sa.JSON(), nullable=True))
    op.add_column("bookings", sa.Column("signed_snapshot_sha256", sa.String(64), nullable=True))

    op.create_table(
        "contract_templates",
        sa.Column("template_id", sa.String(36), primary_key=True),
        sa.Column(
            "vendor_id", sa.String(36),
            sa.ForeignKey("vendors.vendor_id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_contract_templates_vendor_id", "contract_templates", ["vendor_id"])

    op.create_table(
        "contract_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column(
            "booking_id", sa.String(36),
            sa.ForeignKey("bookings.booking_id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("actor", sa.String(20), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
    )
    op.create_index("ix_contract_events_booking_id", "contract_events", ["booking_id"])


def downgrade():
    op.drop_index("ix_contract_events_booking_id", table_name="contract_events")
    op.drop_table("contract_events")
    op.drop_index("ix_contract_templates_vendor_id", table_name="contract_templates")
    op.drop_table("contract_templates")
    op.drop_column("bookings", "signed_snapshot_sha256")
    op.drop_column("bookings", "signed_snapshot")
    op.drop_column("bookings", "revision")
    op.drop_column("bookings", "terms_clauses")
    op.drop_column("bookings", "payment_schedule")
    op.drop_column("bookings", "discount_cents")
    op.drop_column("bookings", "line_items")
