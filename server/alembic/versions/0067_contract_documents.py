"""contract documents: the editor's layout, template kinds, attached addenda

Revision ID: 0067_contract_documents
Revises: 0066_leads_pipeline
Create Date: 2026-10-01

The vendor web app's document-style contract editor (redesign step 7b,
docs/DECISIONS.md #21):

- bookings.document_title, bookings.document_layout — the agreement's title
  and its blocks in order (terms sections with their text, and where the
  structured blocks — event, items, schedule, signature — sit among them).
  Terms sections are still mirrored into terms_clauses, which the signing
  page reads.
- contract_templates.kind — agreement | addendum | cancellation.
- contract_documents — an addendum or cancellation agreement attached to a
  signed booking: text only, its own link and signature, no effect on the
  booking's price, date or hold.

Additive only.
"""
from alembic import op
import sqlalchemy as sa

revision = "0067_contract_documents"
down_revision = "0066_leads_pipeline"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bookings", sa.Column("document_title", sa.String(200), nullable=True))
    op.add_column("bookings", sa.Column("document_layout", sa.JSON(), nullable=True))
    op.add_column(
        "contract_templates",
        sa.Column("kind", sa.String(20), nullable=False, server_default="agreement"),
    )
    op.create_table(
        "contract_documents",
        sa.Column("document_id", sa.String(36), primary_key=True),
        sa.Column(
            "booking_id", sa.String(36),
            sa.ForeignKey("bookings.booking_id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("vendor_id", sa.String(36), sa.ForeignKey("vendors.vendor_id"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("sections", sa.JSON(), nullable=False),
        sa.Column("token", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("viewed_at", sa.DateTime(), nullable=True),
        sa.Column("signed_at", sa.DateTime(), nullable=True),
        sa.Column("signer_name", sa.String(255), nullable=True),
        sa.Column("declined_at", sa.DateTime(), nullable=True),
        sa.Column("decline_reason", sa.String(500), nullable=True),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("signed_snapshot", sa.JSON(), nullable=True),
        sa.Column("signed_snapshot_sha256", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_contract_documents_booking_id", "contract_documents", ["booking_id"])
    op.create_index("ix_contract_documents_token", "contract_documents", ["token"])


def downgrade():
    op.drop_index("ix_contract_documents_token", table_name="contract_documents")
    op.drop_index("ix_contract_documents_booking_id", table_name="contract_documents")
    op.drop_table("contract_documents")
    op.drop_column("contract_templates", "kind")
    op.drop_column("bookings", "document_layout")
    op.drop_column("bookings", "document_title")
