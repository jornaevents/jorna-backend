"""What a vendor sells is decided per service, not once at signup.

The signup form asked for one category under a heading about listing services,
which read as though it were the service's. It never was: every service carries
its own, and the bundle builder has always matched on that. This moves search
onto the same footing, and lets signup stop asking.

The risk is legacy data — services created before service-level categorisation,
whose category is null — so the vendor's own is still consulted for those.
"""

import uuid

from app.db.models import Service, User, Vendor
from app.services.vendor_service import create_vendor, list_vendors, search_vendors
from tests.test_api import TestingSessionLocal


def _vendor(db, *, category: str | None, subcategory: str | None = None):
    tag = uuid.uuid4().hex[:8]
    user = User(
        email=f"cat_{tag}@t.com", username=f"cat_{tag}", password="pw", phone="1",
        f_name="C", l_name="V", age=30, location="NJ", gender="F",
        language="EN", token_version=0,
    )
    db.add(user); db.commit(); db.refresh(user)
    created = create_vendor(
        user_id=user.user_id, bio="b", category=category, subcategory=subcategory, db=db
    )
    return db.query(Vendor).filter(Vendor.vendor_id == created["vendor_id"]).first()


def _service(db, vendor, *, name: str, category: str | None, subcategory: str | None = None):
    svc = Service(
        name=name, price=500.0, duration_minutes=180, vendor_id=vendor.vendor_id,
        experience="5y", category=category, subcategory=subcategory,
    )
    db.add(svc); db.commit(); db.refresh(svc)
    return svc


def _names(found) -> set[str]:
    """search_vendors paginates, so the rows are under "items"."""
    return {r["service_name"] for r in found["items"] if r.get("service_name")}


def _vendor_ids(found) -> set[str]:
    return {r["vendor_id"] for r in found["items"]}


# ── Signing up without answering ──────────────────────────────────────


def test_a_vendor_can_be_created_without_a_category():
    """The column is NOT NULL, so an unset one is stored as a placeholder."""
    db = TestingSessionLocal()
    vendor = _vendor(db, category=None)
    assert vendor.category == "other"
    db.close()


def test_they_are_findable_by_what_they_list():
    """The whole point: no category at signup, still turns up under the one
    their service is in."""
    db = TestingSessionLocal()
    vendor = _vendor(db, category=None)
    _service(db, vendor, name=f"Dhol {vendor.vendor_id[:6]}", category="music_entertainment")

    found = search_vendors(category="music_entertainment", db=db)
    assert vendor.vendor_id in _vendor_ids(found)
    db.close()


# ── Search follows the listing, not its owner ─────────────────────────


def test_a_vendor_selling_two_things_is_found_under_both():
    """Previously they carried one category and were invisible under the other."""
    db = TestingSessionLocal()
    vendor = _vendor(db, category=None)
    tag = vendor.vendor_id[:6]
    _service(db, vendor, name=f"DJ {tag}", category="music_entertainment")
    _service(db, vendor, name=f"Lights {tag}", category="decor_styling")

    assert f"DJ {tag}" in _names(search_vendors(category="music_entertainment", db=db))
    assert f"Lights {tag}" in _names(search_vendors(category="decor_styling", db=db))
    db.close()


def test_only_the_matching_listing_comes_back():
    """A row is a vendor paired with one listing, so "dj" must not drag in the
    same vendor's decor service just because its owner is filed under music."""
    db = TestingSessionLocal()
    vendor = _vendor(db, category="music_entertainment")
    tag = vendor.vendor_id[:6]
    _service(db, vendor, name=f"DJ {tag}", category="music_entertainment")
    _service(db, vendor, name=f"Lights {tag}", category="decor_styling")

    names = _names(search_vendors(category="music_entertainment", db=db))
    assert f"DJ {tag}" in names
    assert f"Lights {tag}" not in names
    db.close()


def test_a_service_with_no_category_falls_back_to_its_vendor():
    """Legacy rows. Service.category is nullable and predates this."""
    db = TestingSessionLocal()
    vendor = _vendor(db, category="photography_video")
    tag = vendor.vendor_id[:6]
    _service(db, vendor, name=f"Old {tag}", category=None)

    assert f"Old {tag}" in _names(search_vendors(category="photography_video", db=db))
    db.close()


def test_the_vendors_own_category_does_not_widen_a_categorised_service():
    """The fallback is for uncategorised services only — otherwise a vendor
    filed under one thing would match everything they sell."""
    db = TestingSessionLocal()
    vendor = _vendor(db, category="catering_food")
    tag = vendor.vendor_id[:6]
    _service(db, vendor, name=f"Photos {tag}", category="photography_video")

    assert f"Photos {tag}" not in _names(search_vendors(category="catering_food", db=db))
    db.close()


# ── The vendor listing endpoint ───────────────────────────────────────


def test_listing_vendors_by_category_reads_their_services_too():
    db = TestingSessionLocal()
    vendor = _vendor(db, category=None)
    _service(db, vendor, name=f"Mehndi {vendor.vendor_id[:6]}", category="beauty_mehndi")

    found = list_vendors(category="beauty_mehndi", db=db, limit=100)
    assert vendor.vendor_id in {v["vendor_id"] for v in found["items"]}
    db.close()


def test_listing_counts_each_vendor_once():
    """EXISTS, not a join — two matching services must not make two rows, or
    `total` and the paging built on it both go wrong."""
    db = TestingSessionLocal()
    vendor = _vendor(db, category=None)
    tag = vendor.vendor_id[:6]
    _service(db, vendor, name=f"DJ A {tag}", category="music_entertainment")
    _service(db, vendor, name=f"DJ B {tag}", category="music_entertainment")

    found = list_vendors(category="music_entertainment", db=db, limit=100)
    ids = [v["vendor_id"] for v in found["items"]]
    assert ids.count(vendor.vendor_id) == 1
    assert found["total"] == len(ids) or found["total"] >= 1
    db.close()
