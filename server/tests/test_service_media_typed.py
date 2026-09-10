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

from app.db.models import Service, User, Vendor
from app.routers.vendors import InstagramEnrichRequest, instagram_enrich
from app.routers.services import _count_media
from app.services.service_service import media_url
from app.services.vendor_service import create_vendor
from tests.test_api import TestingSessionLocal


def _vendor(db):
    tag = uuid.uuid4().hex[:8]
    user = User(
        email=f"ig_{tag}@t.com", username=f"ig_{tag}", password="pw", phone="1",
        f_name="I", l_name="G", age=30, location="NJ", gender="F",
        language="EN", token_version=0,
    )
    db.add(user); db.commit(); db.refresh(user)
    created = create_vendor(user_id=user.user_id, bio="b", category="photography_video", db=db)
    return db.query(Vendor).filter(Vendor.vendor_id == created["vendor_id"]).first()


def _service(db, vendor, *, media=None):
    svc = Service(
        name="Full Day Coverage", price=2000.0, duration_minutes=480,
        vendor_id=vendor.vendor_id, experience="5y", category="photography_video",
        media=media,
    )
    db.add(svc); db.commit(); db.refresh(svc)
    return svc


# ── _count_media ignores entries with no real URL ─────────────────────


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


# ── Instagram enrichment writes the typed shape ────────────────────────


def test_instagram_enrich_writes_typed_media_not_bare_strings():
    db = TestingSessionLocal()
    vendor = _vendor(db)
    service = _service(db, vendor)

    instagram_enrich(
        vendor_id=vendor.vendor_id,
        body=InstagramEnrichRequest(tags=[], images=["https://instagram.cdn/post1.jpg"], bio=None),
        current_admin=None,
        db=db,
    )

    db.refresh(service)
    assert service.media == [
        {"url": "https://instagram.cdn/post1.jpg", "type": "image", "thumbnail_url": None}
    ]
    db.close()


def test_instagram_enrich_dedupes_against_existing_typed_media():
    """Comparing a scraped URL against existing entries has to read through
    the typed shape (media_url), not compare a bare string to a dict — that
    comparison silently never matches, which is what let duplicates pile up."""
    db = TestingSessionLocal()
    vendor = _vendor(db)
    service = _service(db, vendor, media=[
        {"url": "https://instagram.cdn/post1.jpg", "type": "image", "thumbnail_url": None},
    ])

    instagram_enrich(
        vendor_id=vendor.vendor_id,
        body=InstagramEnrichRequest(
            tags=[],
            images=["https://instagram.cdn/post1.jpg", "https://instagram.cdn/post2.jpg"],
            bio=None,
        ),
        current_admin=None,
        db=db,
    )

    db.refresh(service)
    assert [media_url(m) for m in service.media] == [
        "https://instagram.cdn/post1.jpg",
        "https://instagram.cdn/post2.jpg",
    ]
    db.close()


def test_instagram_enrich_still_caps_media_at_nine():
    db = TestingSessionLocal()
    vendor = _vendor(db)
    existing = [
        {"url": f"https://cdn/existing{i}.jpg", "type": "image", "thumbnail_url": None}
        for i in range(7)
    ]
    service = _service(db, vendor, media=existing)

    instagram_enrich(
        vendor_id=vendor.vendor_id,
        body=InstagramEnrichRequest(
            tags=[],
            images=[f"https://instagram.cdn/post{i}.jpg" for i in range(5)],
            bio=None,
        ),
        current_admin=None,
        db=db,
    )

    db.refresh(service)
    assert len(service.media) == 9
    assert all(media_url(m) for m in service.media)
    db.close()
