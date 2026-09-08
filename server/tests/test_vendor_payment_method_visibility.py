"""Phase 3 of "optional escrow": a client needs to see whether a vendor is
on the protected (Stripe) or direct (manual) track before booking, so
payment_method has to be public — on the single-vendor detail, the plain
listing, and search results. venmo_handle/zelle_contact stay booking-scoped
(see bundle_service._booking_summary); this only covers the public flag.
"""

import uuid

from app.db.models import Service, User, Vendor
from app.services.vendor_service import create_vendor, get_vendor, list_vendors, search_vendors
from tests.test_api import TestingSessionLocal


def _manual_vendor(db):
    tag = uuid.uuid4().hex[:8]
    user = User(
        email=f"pmv_{tag}@t.com", username=f"pmv_{tag}", password="pw", phone="1",
        f_name="P", l_name="V", age=30, location="NJ", gender="F",
        language="EN", token_version=0,
    )
    db.add(user); db.commit(); db.refresh(user)
    created = create_vendor(user_id=user.user_id, bio="b", category="venue", db=db)
    vendor = db.query(Vendor).filter(Vendor.vendor_id == created["vendor_id"]).first()
    vendor.payment_method = "manual"
    vendor.venmo_handle = "@should-not-leak"
    db.commit()
    db.refresh(vendor)
    return vendor


def test_get_vendor_exposes_payment_method_but_not_handles():
    db = TestingSessionLocal()
    vendor = _manual_vendor(db)
    try:
        result = get_vendor(vendor_id=vendor.vendor_id, db=db)
        assert result["payment_method"] == "manual"
        assert "venmo_handle" not in result
        assert "zelle_contact" not in result
    finally:
        db.close()


def test_get_vendor_defaults_to_stripe():
    db = TestingSessionLocal()
    tag = uuid.uuid4().hex[:8]
    user = User(
        email=f"pmv_default_{tag}@t.com", username=f"pmv_default_{tag}", password="pw", phone="1",
        f_name="D", l_name="V", age=30, location="NJ", gender="F",
        language="EN", token_version=0,
    )
    db.add(user); db.commit(); db.refresh(user)
    created = create_vendor(user_id=user.user_id, bio="b", category="venue", db=db)
    try:
        result = get_vendor(vendor_id=created["vendor_id"], db=db)
        assert result["payment_method"] == "stripe"
    finally:
        db.close()


def test_list_vendors_exposes_payment_method():
    """category="venue" keeps the result set narrow — an unfiltered list_vendors
    call picks up every vendor the whole suite has created by this point, and
    the default limit isn't guaranteed to reach a vendor created this late."""
    db = TestingSessionLocal()
    vendor = _manual_vendor(db)
    try:
        found = list_vendors(db=db, category="venue", limit=100, offset=0)
        row = next(v for v in found["items"] if v["vendor_id"] == vendor.vendor_id)
        assert row["payment_method"] == "manual"
    finally:
        db.close()


def test_search_vendors_exposes_payment_method():
    db = TestingSessionLocal()
    vendor = _manual_vendor(db)
    svc = Service(
        name=f"Manual Venue {vendor.vendor_id[:6]}", price=500.0, duration_minutes=180,
        vendor_id=vendor.vendor_id, experience="5y", category="venue",
    )
    db.add(svc); db.commit(); db.refresh(svc)
    try:
        found = search_vendors(category="venue", db=db)
        row = next(r for r in found["items"] if r["vendor_id"] == vendor.vendor_id)
        assert row["payment_method"] == "manual"
    finally:
        db.close()
