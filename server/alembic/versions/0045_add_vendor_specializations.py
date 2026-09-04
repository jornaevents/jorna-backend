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

This revision branched off 0044 in parallel with the real chain (0046+
all descend from 0045_performer_pricing instead) and was never merged back
in — see 0052_merge_heads. Production's own column already exists: this
file's upgrade() was run against it directly before that branching was
noticed, without ever being recorded in alembic_version there, so
reconciling the two heads means Alembic will run this upgrade() for the
first time against a database that already has the column. IF NOT EXISTS
makes that safe; a plain add_column would fail there with "column already
exists". A genuinely fresh database (nothing has ever run this) still gets
the column exactly as before.
"""
from alembic import op

revision = "0045_vendor_specializations"
down_revision = "0044_booking_client_note"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS specializations JSON")


def downgrade():
    op.execute("ALTER TABLE vendors DROP COLUMN IF EXISTS specializations")
