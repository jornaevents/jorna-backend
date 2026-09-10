"""re-backfill service.media rows the Instagram scraper wrote after 0043

Revision ID: 0056_rebackfill_service_media
Revises: 0055_manual_payment_attestation
Create Date: 2026-09-10

0043_typed_service_media backfilled every services.media row from the old
bare-string shape to {"url", "type", "thumbnail_url"}, on the stated premise
that "every entry uploaded going forward is written in the new shape
directly." That premise was wrong: the Instagram-enrichment write paths
(app/routers/admin.py's run_scraper, app/routers/vendors.py's
instagram-enrich) kept appending bare URL strings after 0043 shipped, for any
vendor whose Instagram got (re-)scraped since.

Those bare strings are invisible in both clients — the frontend's MediaItem
type expects an object and silently drops anything without a `.url`
property — while still counting toward the 10-image-per-service cap on the
backend, since Service.media is untyped JSON and Python's duck-typing let a
plain string through the count unnoticed. A vendor could be told a service
"already has 9 images" with only 2 (their own, properly-typed uploads)
visible anywhere. See app/routers/services.py's MAX_IMAGES_PER_SERVICE check
and _count_media.

Both write paths are now fixed to write the typed shape directly (same
change, same commit as this migration). This migration is the one-time catch
-up for whatever they already wrote in the meantime — identical logic to
0043's upgrade(), safe to run again: it only touches rows that still have an
untyped (string) entry, so a row 0043 already covered, or one this migration
already fixed on a prior run, is left alone.
"""
from alembic import op
import sqlalchemy as sa
import json

revision = "0056_rebackfill_service_media"
down_revision = "0055_manual_payment_attestation"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT service_id, media FROM services WHERE media IS NOT NULL")
    ).fetchall()

    for service_id, media in rows:
        if not media:
            continue
        # media comes back already deserialized (json/jsonb via the driver).
        # Only rewrite rows that actually have an old-shape (string) entry —
        # leaves already-typed rows, and rows from a retried run, untouched.
        if not any(isinstance(item, str) for item in media):
            continue
        typed = [
            item if isinstance(item, dict)
            else {"url": item, "type": "image", "thumbnail_url": None}
            for item in media
        ]
        conn.execute(
            sa.text("UPDATE services SET media = CAST(:media AS json) WHERE service_id = :service_id"),
            {"media": json.dumps(typed), "service_id": service_id},
        )


def downgrade():
    # Nothing to revert on its own — this migration only ever turns a bare
    # string into the equivalent typed dict, which 0043's own downgrade
    # already knows how to unwind (it re-derives from whatever shape a row is
    # in at downgrade time, not from what this migration specifically
    # changed). Making this a no-op keeps that logic in the one place that
    # already owns it instead of duplicating it here.
    pass
