"""One-time email codes for signing by link (docs/DECISIONS.md #27)

Revision ID: 0073_signing_codes
Revises: 0072_field_negotiation
Create Date: 2026-10-07

Before a client signs a contract or an attached document by link, a
6-digit code goes to the email on the contract; the signature carries
proof they could read it.

- signing_codes: one row per code sent, hash only, bound to the address it
  went to. document_id is null for the contract itself.

Additive only. The rest of the signing evidence (address, browser,
consent) lives in the existing signed_snapshot JSON.
"""
from alembic import op
import sqlalchemy as sa

revision = "0073_signing_codes"
down_revision = "0072_field_negotiation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "signing_codes",
        sa.Column("code_id", sa.String(36), primary_key=True),
        sa.Column(
            "booking_id", sa.String(36),
            sa.ForeignKey("bookings.booking_id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "document_id", sa.String(36),
            sa.ForeignKey("contract_documents.document_id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_signing_codes_booking_id", "signing_codes", ["booking_id"])


def downgrade():
    op.drop_index("ix_signing_codes_booking_id", table_name="signing_codes")
    op.drop_table("signing_codes")
