"""remember which Google Calendar scopes a vendor actually granted

Revision ID: 0049_google_granted_scopes
Revises: 0048_message_digest_at
Create Date: 2026-09-03

SCOPES (app/utils/calendar.py) is growing to add calendar.events (write
access, for two-way sync — booking write-back is landing in a follow-up
PR). Google does not silently widen an existing OAuth grant when the app
starts asking for more, so every vendor who connected before this column
existed holds a token scoped to calendar.readonly only. Recording what was
actually granted, not assumed, lets a write path check before attempting
anything rather than guess from SCOPES what a given vendor's token covers.

Data-only in effect: no schema change beyond the new nullable column, no
backfill — NULL correctly means "connected before this existed, read-only
until they reconnect," which is exactly the safe default for every
existing row.
"""
from alembic import op
import sqlalchemy as sa

revision = "0049_google_granted_scopes"
down_revision = "0048_message_digest_at"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "vendors",
        sa.Column("google_granted_scopes", sa.Text, nullable=True),
    )


def downgrade():
    op.drop_column("vendors", "google_granted_scopes")
