"""Vendor availability enforcement: a vendor serves one event per day, so two
approved/paid bookings may not overlap in date.

Covers the shared conflict helper, the approval guard, the checkout guard, and
the AI-builder's booked-vendor exclusion (regression for the dead "confirmed"
status filter).
"""

import uuid

import pytest

from app.db.models import Booking, Service, User, Vendor
from app.models.chatbot_schemas import ChatbotState, DateRange
from app.services.booking_service import vendor_has_conflicting_booking
from app.services.chatbot_service import _get_booked_vendor_ids
from tests.test_api import TestingSessionLocal, client, make_auth_headers


@pytest.fixture
def seeded_db():
    """One client, one vendor (+owner), one service — plus a second client so we
    can create a competing booking against the same vendor."""
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    def _user(prefix):
        u = User(
            email=f"{prefix}_{uid}@test.com", username=f"{prefix}_{uid}",
            password="pw", phone="1", f_name="A", l_name="B",
            age=20, location="123", gender="M", language="EN", token_version=0,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        return u

    user = _user("avail_client")
    other_user = _user("avail_client2")
    vendor_user = _user("avail_vendor")

    vendor = Vendor(user_id=vendor_user.user_id, bio="bio", rating=5.0, num_events=1)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    service = Service(name="Test Service", price=100.0, vendor_id=vendor.vendor_id, experience="none")
    db.add(service)
    db.commit()
    db.refresh(service)

    yield {
        "user": user, "other_user": other_user, "vendor_user": vendor_user,
        "vendor": vendor, "service": service, "db": db,
    }
    db.close()


def _booking(db, *, user_id, vendor_id, service_id, date_iso, status,
             date_end=None, bundle_id=None):
    bk = Booking(
        user_id=user_id, vendor_id=vendor_id, service_id=service_id,
        time_start="10:00", time_end="12:00", location="145 Main St",
        date_iso=date_iso, date_end=date_end, status=status, bundle_id=bundle_id,
    )
    db.add(bk)
    db.commit()
    db.refresh(bk)
    return bk


# ── vendor_has_conflicting_booking ────────────────────────────────────

def test_conflict_helper_detects_same_day(seeded_db):
    db, vendor, service = seeded_db["db"], seeded_db["vendor"], seeded_db["service"]
    _booking(db, user_id=seeded_db["user"].user_id, vendor_id=vendor.vendor_id,
             service_id=service.service_id, date_iso="2026-09-01", status="approved")

    conflict = vendor_has_conflicting_booking(
        vendor_id=vendor.vendor_id, date_iso="2026-09-01", date_end=None, db=db,
    )
    assert conflict is not None


def test_conflict_helper_ignores_other_days(seeded_db):
    db, vendor, service = seeded_db["db"], seeded_db["vendor"], seeded_db["service"]
    _booking(db, user_id=seeded_db["user"].user_id, vendor_id=vendor.vendor_id,
             service_id=service.service_id, date_iso="2026-09-01", status="approved")

    assert vendor_has_conflicting_booking(
        vendor_id=vendor.vendor_id, date_iso="2026-09-05", date_end=None, db=db,
    ) is None


def test_conflict_helper_ignores_pending_and_rejected(seeded_db):
    """Only approved/paid bookings lock a date — a pending lead or a rejected
    request does not."""
    db, vendor, service = seeded_db["db"], seeded_db["vendor"], seeded_db["service"]
    _booking(db, user_id=seeded_db["user"].user_id, vendor_id=vendor.vendor_id,
             service_id=service.service_id, date_iso="2026-09-01", status="pending")
    _booking(db, user_id=seeded_db["user"].user_id, vendor_id=vendor.vendor_id,
             service_id=service.service_id, date_iso="2026-09-01", status="rejected")

    assert vendor_has_conflicting_booking(
        vendor_id=vendor.vendor_id, date_iso="2026-09-01", date_end=None, db=db,
    ) is None


def test_conflict_helper_multiday_overlap(seeded_db):
    """A multi-day booking blocks any day inside its range."""
    db, vendor, service = seeded_db["db"], seeded_db["vendor"], seeded_db["service"]
    _booking(db, user_id=seeded_db["user"].user_id, vendor_id=vendor.vendor_id,
             service_id=service.service_id, date_iso="2026-09-01",
             date_end="2026-09-03", status="payment_confirmed")

    # A single-day request landing inside the range conflicts.
    assert vendor_has_conflicting_booking(
        vendor_id=vendor.vendor_id, date_iso="2026-09-02", date_end=None, db=db,
    ) is not None
    # A request just after the range is free.
    assert vendor_has_conflicting_booking(
        vendor_id=vendor.vendor_id, date_iso="2026-09-04", date_end=None, db=db,
    ) is None


def test_conflict_helper_excludes_self(seeded_db):
    db, vendor, service = seeded_db["db"], seeded_db["vendor"], seeded_db["service"]
    bk = _booking(db, user_id=seeded_db["user"].user_id, vendor_id=vendor.vendor_id,
                  service_id=service.service_id, date_iso="2026-09-01", status="approved")

    assert vendor_has_conflicting_booking(
        vendor_id=vendor.vendor_id, date_iso="2026-09-01", date_end=None, db=db,
        exclude_booking_id=bk.booking_id,
    ) is None


# ── Approval guard ────────────────────────────────────────────────────

def test_vendor_cannot_approve_conflicting_date(seeded_db):
    db, vendor, service = seeded_db["db"], seeded_db["vendor"], seeded_db["service"]
    # Client A already locked in this vendor for 2026-09-01.
    _booking(db, user_id=seeded_db["user"].user_id, vendor_id=vendor.vendor_id,
             service_id=service.service_id, date_iso="2026-09-01", status="approved")
    # Client B has a pending request for the same vendor + date.
    pending = _booking(db, user_id=seeded_db["other_user"].user_id, vendor_id=vendor.vendor_id,
                       service_id=service.service_id, date_iso="2026-09-01", status="pending")

    resp = client.put(
        f"/bookings/{pending.booking_id}/status",
        json={"status": "approved"},
        headers=make_auth_headers(seeded_db["vendor_user"]),
    )
    assert resp.status_code == 409
    db.refresh(pending)
    assert pending.status == "pending"  # unchanged


def test_vendor_can_approve_non_overlapping_date(seeded_db):
    db, vendor, service = seeded_db["db"], seeded_db["vendor"], seeded_db["service"]
    _booking(db, user_id=seeded_db["user"].user_id, vendor_id=vendor.vendor_id,
             service_id=service.service_id, date_iso="2026-09-01", status="approved")
    pending = _booking(db, user_id=seeded_db["other_user"].user_id, vendor_id=vendor.vendor_id,
                       service_id=service.service_id, date_iso="2026-09-08", status="pending")

    resp = client.put(
        f"/bookings/{pending.booking_id}/status",
        json={"status": "approved"},
        headers=make_auth_headers(seeded_db["vendor_user"]),
    )
    assert resp.status_code == 200
    db.refresh(pending)
    assert pending.status == "approved"


# ── Checkout guard ────────────────────────────────────────────────────

def test_checkout_blocked_when_vendor_taken(seeded_db):
    """If the vendor's date was locked by another booking after this one was
    approved, paying is refused with 409 (before any Stripe call)."""
    db, vendor, service = seeded_db["db"], seeded_db["vendor"], seeded_db["service"]
    # A competing booking for the same date is already paid.
    _booking(db, user_id=seeded_db["user"].user_id, vendor_id=vendor.vendor_id,
             service_id=service.service_id, date_iso="2026-09-01", status="payment_confirmed")
    # This client's booking got approved for the same date (race).
    mine = _booking(db, user_id=seeded_db["other_user"].user_id, vendor_id=vendor.vendor_id,
                    service_id=service.service_id, date_iso="2026-09-01", status="approved")

    resp = client.post(
        f"/payments/bookings/{mine.booking_id}/checkout-session",
        headers=make_auth_headers(seeded_db["other_user"]),
    )
    assert resp.status_code == 409


# ── AI builder exclusion (regression for the dead "confirmed" filter) ──

def test_ai_builder_excludes_approved_vendor(seeded_db):
    db, vendor, service = seeded_db["db"], seeded_db["vendor"], seeded_db["service"]
    _booking(db, user_id=seeded_db["user"].user_id, vendor_id=vendor.vendor_id,
             service_id=service.service_id, date_iso="2026-10-15", status="approved")

    state = ChatbotState(event_date="2026-10-15")
    assert vendor.vendor_id in _get_booked_vendor_ids(state, db)

    # A range that misses the booked day leaves the vendor available.
    free = ChatbotState(date_range=DateRange(start="2026-11-01", end="2026-11-03"))
    assert vendor.vendor_id not in _get_booked_vendor_ids(free, db)
