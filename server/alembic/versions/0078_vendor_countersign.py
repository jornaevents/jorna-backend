"""The vendor countersigns a contract after the client (docs/DECISIONS.md #34)

Revision ID: 0078_vendor_countersign
Revises: 0077_lead_inquiry_fields
Create Date: 2026-10-19

- bookings / contract_documents.client_signed_at: when the client signed.
  signed_at now means both sides have signed; until the vendor does, the
  contract is "awaiting_vendor" with client_signed_at set and signed_at null.
- …vendor_signer_name / vendor_signed_at / vendor_sign_evidence: the vendor's
  typed name, when, and where from (also inside the signed snapshot).
- vendors.esign_consent_version / esign_consent_at: the e-records consent a
  vendor gave, asked once per wording.

Existing signatures were the client's alone, so client_signed_at = signed_at.
Their vendor fields stay empty: a contract signed before this keeps its
"issued this agreement" line rather than a countersignature.
"""
from alembic import op
import sqlalchemy as sa

revision = "0078_vendor_countersign"
down_revision = "0077_lead_inquiry_fields"
branch_labels = None
depends_on = None

TABLES = ("bookings", "contract_documents")


def upgrade():
    for table in TABLES:
        op.add_column(table, sa.Column("client_signed_at", sa.DateTime(), nullable=True))
        op.add_column(table, sa.Column("vendor_signer_name", sa.String(255), nullable=True))
        op.add_column(table, sa.Column("vendor_signed_at", sa.DateTime(), nullable=True))
        op.add_column(table, sa.Column("vendor_sign_evidence", sa.JSON(), nullable=True))
        op.execute(f"UPDATE {table} SET client_signed_at = signed_at WHERE signed_at IS NOT NULL")
    op.add_column("vendors", sa.Column("esign_consent_version", sa.String(20), nullable=True))
    op.add_column("vendors", sa.Column("esign_consent_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("vendors", "esign_consent_at")
    op.drop_column("vendors", "esign_consent_version")
    for table in TABLES:
        op.drop_column(table, "vendor_sign_evidence")
        op.drop_column(table, "vendor_signed_at")
        op.drop_column(table, "vendor_signer_name")
        op.drop_column(table, "client_signed_at")
