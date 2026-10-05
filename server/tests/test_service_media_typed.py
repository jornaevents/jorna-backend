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


# ── Instagram enrichment never touches packages ───────────────────────
# It used to add post images to the vendor's first package. The vendor never
# chose them and Instagram's CDN URLs expire within days, so they became
# broken images; both enrichment paths now write tags (and an empty bio) only.


def test_instagram_enrich_ignores_images_and_leaves_the_package_alone():
    db = TestingSessionLocal()
    vendor = _vendor(db)
    own = [{"url": "https://cdn/own.jpg", "type": "image", "thumbnail_url": None}]
    service = _service(db, vendor, media=own)

    # An older caller may still send "images" — accepted, and ignored.
    body = InstagramEnrichRequest.model_validate(
        {"tags": ["bhangra"], "images": ["https://instagram.cdn/post1.jpg"], "bio": None}
    )
    res = instagram_enrich(vendor_id=vendor.vendor_id, body=body, current_admin=None, db=db)

    db.refresh(service); db.refresh(vendor)
    assert service.media == own
    assert vendor.instagram_tags == ["bhangra"]
    assert res["message"] == "Enriched with 1 tags."
    db.close()


def test_scheduled_scraper_run_leaves_packages_alone(monkeypatch):
    from app.routers import admin
    from tests.test_api import client

    db = TestingSessionLocal()
    vendor = _vendor(db)
    vendor.instagram_username = f"ig_{uuid.uuid4().hex[:8]}"
    db.commit()
    service = _service(db, vendor)
    vendor_id, service_id = vendor.vendor_id, service.service_id
    db.close()

    monkeypatch.setenv("APIFY_API_TOKEN", "apify-test")
    monkeypatch.setenv("SCRAPER_API_KEY", "k")
    monkeypatch.setattr(admin.time, "sleep", lambda _s: None)
    monkeypatch.setattr(admin, "_scrape_profile", lambda _t, u: ({
        "biography": "",
        "posts": [{"caption": "#sangeet", "displayUrl": f"https://instagram.cdn/{u}.jpg"}],
    }, None))

    res = client.post("/admin/scraper/run", headers={"X-Scraper-Key": "k"})

    assert res.status_code == 200
    mine = [r for r in res.json()["results"] if r["vendor_id"] == vendor_id]
    assert mine and mine[0]["status"] == "enriched" and "images" not in mine[0]
    db = TestingSessionLocal()
    assert db.query(Service).filter(Service.service_id == service_id).first().media is None
    assert "sangeet" in db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first().instagram_tags
    db.close()
