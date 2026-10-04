"""vendors.default_payment_plan: the payment schedule a vendor usually offers

Revision ID: 0070_default_payment_plan
Revises: 0069_contract_drafts
Create Date: 2026-10-03

{"preset": "full" | "deposit_balance" | "three", "balance_days_before": int}.
New contracts in the builder and "Send with my usual terms" start from it,
instead of the vendor re-picking a preset on every contract. Null keeps the
old behaviour: deposit + balance when the vendor takes a deposit, else pay
in full.

Additive only.
"""
from alembic import op
import sqlalchemy as sa

revision = "0070_default_payment_plan"
down_revision = "0069_contract_drafts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("vendors", sa.Column("default_payment_plan", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("vendors", "default_payment_plan")
