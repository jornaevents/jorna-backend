"""When a lead was last emailed the vendor's inquiry link

Revision ID: 0092_lead_inquiry_link
Revises: 0091_signup_side
Create Date: 2026-11-18

- leads.inquiry_link_sent_at: a vendor can email a lead they typed in their
  inquiry form link (DECISIONS #56). The address is whatever the vendor
  typed, so it's limited to once a day per lead; this is what the limit
  reads. NULL until the first one.
"""
import sqlalchemy as sa

from alembic import op

revision = "0092_lead_inquiry_link"
down_revision = "0091_signup_side"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("inquiry_link_sent_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "inquiry_link_sent_at")
