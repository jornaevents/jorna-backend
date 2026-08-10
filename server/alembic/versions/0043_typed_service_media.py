"""give each service media entry a type, so photos and videos can share one list

Revision ID: 0043_typed_service_media
Revises: 0042_conversation_subjects
Create Date: 2026-08-10

Service.media used to be a bare list[str] of image URLs — every consumer,
frontend and backend alike, assumed "media" meant "photo". Video support
needs to tell the two apart (a <video> tag can't render a photo URL and vice
versa) and needs somewhere to hang a video's poster-frame thumbnail, so each
entry becomes {"url", "type": "image"|"video", "thumbnail_url"}.

This backfills existing rows from the old shape to the new one. Every entry
uploaded going forward is written in the new shape directly (see
app/routers/services.py); this migration exists only for services.media rows
that predate it.
"""
from alembic import op
import sqlalchemy as sa
import json

revision = "0043_typed_service_media"
down_revision = "0042_conversation_subjects"
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
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT service_id, media FROM services WHERE media IS NOT NULL")
    ).fetchall()

    for service_id, media in rows:
        if not media:
            continue
        # Videos have no representation in the old shape — dropped, not
        # merely un-typed, since a bare URL to a video file is not what any
        # pre-migration reader expected to receive.
        urls = [
            item["url"] if isinstance(item, dict) else item
            for item in media
            if not (isinstance(item, dict) and item.get("type") == "video")
        ]
        conn.execute(
            sa.text("UPDATE services SET media = CAST(:media AS json) WHERE service_id = :service_id"),
            {"media": json.dumps(urls), "service_id": service_id},
        )
