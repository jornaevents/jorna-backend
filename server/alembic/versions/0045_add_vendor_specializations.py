"""add specializations to vendors

Revision ID: 0045_vendor_specializations
Revises: 0044_booking_client_note
Create Date: 2026-08-29

The frontend onboarding wizard has offered a multi-select of every
category+subcategory a vendor sells under for a while, sent as
`specializations` on POST /vendors and PATCH /vendors/me — but nothing
stored it, so only the first pick (in category/subcategory) ever survived
a reload. Nullable/additive: existing rows stay null and keep reading
through category/subcategory alone.
"""
from alembic import op
import sqlalchemy as sa

revision = "0045_vendor_specializations"
down_revision = "0044_booking_client_note"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("vendors", sa.Column("specializations", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("vendors", "specializations")
