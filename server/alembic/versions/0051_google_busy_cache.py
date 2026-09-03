"""cache Google Calendar busy blocks + track a push-notification channel

Revision ID: 0051_google_busy_cache
Revises: 0050_booking_google_event_id
Create Date: 2026-09-03

get_vendor_availability used to call Google's freebusy API live, on every
single public GET /vendors/{id}/availability request — no caching, straight
external API call on an unauthenticated, likely-high-traffic endpoint. This
adds a per-vendor cache (google_busy_cache/google_busy_synced_at) that a
push-notification channel and a periodic sweep keep warm, so a read is a
column lookup instead of a network call. The channel bookkeeping columns
(google_channel_id/resource_id/expires_at) are what let Google tell Jorna
"something changed" without Jorna having to poll for it.

Chained after 0050_booking_google_event_id (the booking-write-back PR)
purely for migration numbering — alembic's chain is linear, and that PR
claimed 0050 first. The code in this PR has no dependency on write-back;
this just needs to merge after it (or be rebased onto whatever's actually
head at merge time) for a clean single-head history.

Data-only in effect: five new nullable columns, no backfill — every
existing connected vendor correctly starts with no cache and no channel
until the next sync/watch call warms them, matching how a freshly-connected
vendor already needed a first fetch before this change too.
"""
from alembic import op
import sqlalchemy as sa

revision = "0051_google_busy_cache"
down_revision = "0050_booking_google_event_id"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("vendors", sa.Column("google_busy_cache", sa.JSON, nullable=True))
    op.add_column("vendors", sa.Column("google_busy_synced_at", sa.DateTime, nullable=True))
    op.add_column(
        "vendors",
        sa.Column("google_channel_id", sa.String(64), nullable=True, unique=True),
    )
    op.add_column(
        "vendors", sa.Column("google_channel_resource_id", sa.String(255), nullable=True)
    )
    op.add_column(
        "vendors", sa.Column("google_channel_expires_at", sa.DateTime, nullable=True)
    )


def downgrade():
    op.drop_column("vendors", "google_channel_expires_at")
    op.drop_column("vendors", "google_channel_resource_id")
    op.drop_column("vendors", "google_channel_id")
    op.drop_column("vendors", "google_busy_synced_at")
    op.drop_column("vendors", "google_busy_cache")
