"""remember a client's card, so acceptance can charge it

Revision ID: 0035_saved_card
Revises: 0034_backfill_vendor_confirmed
Create Date: 2026-07-29

Payment used to come after acceptance and needed the client to come back for it:
send the plan, wait, and then return to a Pay button per vendor. Plenty of them
never did, and a vendor who had held a date was left waiting on a payment that
was nobody's next action.

The card is collected once, when the plan is sent, and charged when a vendor
accepts. Money still moves at exactly the same moment as before — this changes
where the card details are captured, not when the charge happens.

stripe_customer_id is the Stripe Customer for this user; the payment method is
the card they saved against it. Both nullable: a client who has never sent a
plan has neither, and one whose off-session charge fails still has both.
"""
from alembic import op
import sqlalchemy as sa

revision = "0035_saved_card"
down_revision = "0034_backfill_vendor_confirmed"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("stripe_customer_id", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("stripe_payment_method_id", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("card_brand", sa.String(40), nullable=True))
    op.add_column("users", sa.Column("card_last4", sa.String(4), nullable=True))


def downgrade():
    op.drop_column("users", "card_last4")
    op.drop_column("users", "card_brand")
    op.drop_column("users", "stripe_payment_method_id")
    op.drop_column("users", "stripe_customer_id")
