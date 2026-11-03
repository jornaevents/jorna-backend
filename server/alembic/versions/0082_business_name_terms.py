"""Vendor business name; when an account accepted the terms

Revision ID: 0082_business_name_terms
Revises: 0081_document_line_items
Create Date: 2026-11-03

vendors.business_name is what clients see the vendor called — optional, the
account holder's own name stands in when it's unset (services/names.py).
users.terms_accepted_at records the sign-up form's "I agree to the Terms and
Privacy Policy" box. Both nullable: every existing row stays as it was.
"""
import sqlalchemy as sa
from alembic import op

revision = "0082_business_name_terms"
down_revision = "0081_document_line_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vendors", sa.Column("business_name", sa.String(length=120), nullable=True))
    op.add_column("users", sa.Column("terms_accepted_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "terms_accepted_at")
    op.drop_column("vendors", "business_name")
