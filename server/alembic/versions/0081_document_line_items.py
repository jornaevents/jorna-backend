"""Addenda can change what's sold (docs/DECISIONS.md #38)

Revision ID: 0081_document_line_items
Revises: 0080_service_keys
Create Date: 2026-10-26

An addendum carries optional priced lines to add (line_items) and the ids of
contract lines it takes out (removed_line_ids). Once both sides sign it, the
booking's items, total and payment schedule follow. Both nullable: every
existing document is text only, as before.
"""
import sqlalchemy as sa
from alembic import op

revision = "0081_document_line_items"
down_revision = "0080_service_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contract_documents", sa.Column("line_items", sa.JSON(), nullable=True))
    op.add_column("contract_documents", sa.Column("removed_line_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("contract_documents", "removed_line_ids")
    op.drop_column("contract_documents", "line_items")
