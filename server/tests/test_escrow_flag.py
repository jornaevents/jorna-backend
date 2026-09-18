"""Phase 4 of "optional escrow": the ESCROW_ENABLED kill switch (docs/DECISIONS.md
#12). With it off, every new booking is forced onto the manual Venmo/Zelle
track regardless of what a vendor's profile still says, vendor updates can no
longer select "stripe" or leave both contact fields empty, and Stripe-only
payment endpoints refuse the request instead of reaching Stripe. Each module
imports ESCROW_ENABLED by value, so tests monkeypatch the name where it was
imported to, not app.config.ESCROW_ENABLED itself.
"""

import uuid

from app.db.models import User, Vendor
from tests.test_api import TestingSessionLocal, client, make_auth_headers
from tests.test_bookings import seeded_db  # noqa: F401 -- shared fixture, see test_message_pagination.py


def _register_vendor(prefix: str, *, payment_method: str = "stripe") -> tuple[dict, str]:
    uid = uuid.uuid4().hex[:8]
    db = TestingSessionLocal()
    user = User(
        email=f"{prefix}_{uid}@test.com", username=f"{prefix}_{uid}", password="pw",
        phone="1234567890", f_name="Test", l_name="Vendor", age=30, location="10001",
        gender="Test Gender", language="English", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    headers = make_auth_headers(user)
    db.close()

    resp = client.post("/vendors", json={"bio": "bio"}, headers=headers)
    vendor_id = resp.json()["vendor_id"]
    if payment_method != "stripe":
        db = TestingSessionLocal()
        db.query(Vendor).filter(Vendor.vendor_id == vendor_id).update({"payment_method": payment_method})
        db.commit()
        db.close()
    return headers, vendor_id


def test_new_booking_forced_manual_when_escrow_disabled(monkeypatch, seeded_db):
    """A vendor still stored as "stripe" (nothing migrated) must not produce a
    new stripe-track booking once the flag is off — the override happens at
    booking creation, independent of the vendor row."""
    from app.db.models import Booking as BookingModel
    from app.services import booking_service
    from tests.test_bookings import _future_date

    monkeypatch.setattr(booking_service, "ESCROW_ENABLED", False)

    user = seeded_db["user"]
    service = seeded_db["service"]
    assert seeded_db["vendor"].payment_method == "stripe"

    resp = client.post(
        "/bookings",
        json={
            "service_id": service.service_id,
            "event_name": "Test Event",
            "time_start": "13:00",
            "time_end": "15:00",
            "location": "123 Test St",
            "date_iso": _future_date(180),
            "venue_latitude": 34.0,
            "venue_longitude": -118.0,
        },
        headers=make_auth_headers(user),
    )
    assert resp.status_code == 200, resp.text

    db = TestingSessionLocal()
    booking = db.query(BookingModel).filter(BookingModel.booking_id == resp.json()["booking_id"]).first()
    assert booking.payment_method == "manual"
    db.close()


def test_update_vendor_rejects_stripe_when_escrow_disabled(monkeypatch):
    from app.services import vendor_service

    monkeypatch.setattr(vendor_service, "ESCROW_ENABLED", False)
    headers, _ = _register_vendor("escrow_reject_stripe")

    resp = client.patch("/vendors/me", json={"payment_method": "stripe"}, headers=headers)
    assert resp.status_code == 400, resp.text


def test_update_vendor_requires_a_contact_method_when_escrow_disabled(monkeypatch):
    from app.services import vendor_service

    monkeypatch.setattr(vendor_service, "ESCROW_ENABLED", False)
    headers, _ = _register_vendor("escrow_require_contact")

    # Explicitly setting payment_method with no venmo/zelle on file at all.
    resp = client.patch("/vendors/me", json={"payment_method": "manual"}, headers=headers)
    assert resp.status_code == 400, resp.text

    # Providing one of the two is enough.
    resp = client.patch(
        "/vendors/me",
        json={"payment_method": "manual", "venmo_handle": "@handle"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


def test_update_vendor_unrelated_fields_unblocked_before_payment_info_set(monkeypatch):
    """Regression guard: the requirement above must not fire for a save that
    never touches payment fields, e.g. the identity/bio step of onboarding,
    which runs before a brand-new vendor has any venmo/zelle on file."""
    from app.services import vendor_service

    monkeypatch.setattr(vendor_service, "ESCROW_ENABLED", False)
    headers, _ = _register_vendor("escrow_unrelated_update")

    resp = client.patch("/vendors/me", json={"bio": "Updated bio, no payment info yet"}, headers=headers)
    assert resp.status_code == 200, resp.text


def test_stripe_only_endpoints_refused_when_escrow_disabled(monkeypatch):
    from app.routers import payments as payments_router

    monkeypatch.setattr(payments_router, "ESCROW_ENABLED", False)
    headers, vendor_id = _register_vendor("escrow_gate")

    resp = client.post(f"/payments/vendors/{vendor_id}/stripe-onboard", headers=headers)
    assert resp.status_code == 403, resp.text

    resp = client.get(f"/payments/vendors/{vendor_id}/stripe-status", headers=headers)
    assert resp.status_code == 403, resp.text


def test_manual_track_and_earnings_endpoints_stay_available_when_escrow_disabled(monkeypatch):
    """mark-paid/confirm-received/earnings serve the manual track (or both
    tracks) directly and must not be gated by the flag."""
    from app.routers import payments as payments_router

    monkeypatch.setattr(payments_router, "ESCROW_ENABLED", False)
    headers, vendor_id = _register_vendor("escrow_manual_stays_on", payment_method="manual")

    resp = client.get(f"/payments/vendors/{vendor_id}/earnings", headers=headers)
    assert resp.status_code == 200, resp.text
