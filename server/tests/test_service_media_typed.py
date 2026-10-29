"""Regression coverage for the ghost-media bug.

A vendor was told "this service already has 9 images" while only 2 were
visible anywhere. Root cause: the Instagram-enrichment endpoints
(vendors.py's instagram_enrich, admin.py's run_scraper) appended scraped
image URLs as bare strings instead of the typed {"url", "type",
"thumbnail_url"} shape everything else writes — invisible to the frontend's
MediaItem-typed renderer, but still counted by the backend's
MAX_IMAGES_PER_SERVICE check. See media_url's docstring and
0056_rebackfill_service_media.py.
"""

import uuid
from types import SimpleNamespace

from app.routers.services import _count_media


def test_count_media_ignores_a_blank_url_entry():
    """A failed upload or an incomplete delete can leave a slot with no file
    behind it — that isn't a real photo and shouldn't eat into the cap."""
    service = SimpleNamespace(media=[
        {"url": "https://cdn/a.jpg", "type": "image", "thumbnail_url": None},
        {"url": "", "type": "image", "thumbnail_url": None},
        {"url": None, "type": "image", "thumbnail_url": None},
    ])
    assert _count_media(service, "image") == 1


def test_count_media_still_counts_a_real_bare_string_entry():
    """Legacy/pre-typed rows are still real photos — only an actually-empty
    URL should be excluded, not every un-typed entry."""
    service = SimpleNamespace(media=["https://cdn/legacy.jpg"])
    assert _count_media(service, "image") == 1


# ── Instagram scraping is gone (DECISIONS #36) ────────────────────────
# Vendors can still link a handle; nothing fetches their profile any more.


def test_instagram_scraper_endpoints_are_gone():
    from tests.test_api import client

    assert client.post("/admin/scraper/run", headers={"X-Scraper-Key": "k"}).status_code == 404
    assert client.get("/vendors/instagram-linked").status_code in (404, 405, 422)
    assert client.post(f"/vendors/{uuid.uuid4()}/instagram-enrich", json={"tags": ["x"]}).status_code in (404, 405)
