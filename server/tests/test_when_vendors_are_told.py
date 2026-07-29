"""Vendors hear about a booking when the client sends the plan, not before.

A draft is the client still thinking. Adding a service from the marketplace, or
swapping one out — which is "book the replacement, then remove the original" —
both create bookings, and both used to notify the vendor on the spot: asked to
hold a date for a plan that might not have a date, by a client who might undo it
seconds later with the other half of the swap.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.db.models import Booking, Bundle, Service, User, Vendor
from app.services.booking_service import create_booking
from app.services.bundle_service import select_bundle
from tests.test_api import TestingSessionLocal


@pytest.fixture
def world():
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]
    client_user = User(
        email=f"n_c_{uid}@t.com", username=f"n_c_{uid}", password="pw", phone="1",
        f_name="C", l_name="L", age=30, location="x", gender="M",
        language="EN", token_version=0,
    )
    vendor_user = User(
        email=f"n_v_{uid}@t.com", username=f"n_v_{uid}", password="pw", phone="1",
        f_name="V", l_name="N", age=30, location="x", gender="F",
        language="EN", token_version=0,
    )
    db.add_all([client_user, vendor_user])
    db.commit()
    db.refresh(client_user)
    db.refresh(vendor_user)

    vendor = Vendor(user_id=vendor_user.user_id, bio="b", rating=5.0, num_events=1)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    service = Service(name="DJ", price=500.0, duration_minutes=180,
                      vendor_id=vendor.vendor_id, experience="5y")
    db.add(service)
    db.commit()
    db.refresh(service)

    out = {
        "user_id": client_user.user_id,
        "service_id": service.service_id,
        "vendor_id": vendor.vendor_id,
    }
    db.close()
    yield out


def _bundle(user_id, status):
    db = TestingSessionLocal()
    now = datetime.now(timezone.utc)
    b = Bundle(user_id=user_id, name="Plan", status=status, created_at=now, updated_at=now)
    db.add(b)
    db.commit()
    db.refresh(b)
    bid = b.bundle_id
    db.close()
    return bid


def _book_into(world, bundle_id):
    db = TestingSessionLocal()
    try:
        return create_booking(
            user_id=world["user_id"], service_id=world["service_id"],
            event_name="Wedding", date_iso="2027-06-05", date_end=None,
            time_start="18:00", time_end="23:00", location="12 Maple Ave, Evanston, IL 60201",
            guest_count=100, venue_latitude=None, venue_longitude=None,
            bundle_id=bundle_id, db=db,
        )
    finally:
        db.close()


def test_adding_to_a_draft_tells_nobody(mocker, world):
    """The marketplace's "Book this", and the first half of every swap."""
    dispatch = mocker.patch("app.services.booking_service._dispatch_status_notification")
    res = _book_into(world, _bundle(world["user_id"], "draft"))

    assert dispatch.call_count == 0
    assert "skipped" in res["notification"]


def test_adding_to_a_sent_plan_does(mocker, world):
    """Those vendors are already waiting, and a plan can't have been sent
    without the details — so there's nothing to hold this one back for."""
    dispatch = mocker.patch("app.services.booking_service._dispatch_status_notification")
    _book_into(world, _bundle(world["user_id"], "confirmed"))
    assert dispatch.call_count == 1


def test_booking_a_single_service_starts_a_draft_and_stays_quiet(mocker, world):
    """There is no booking without a plan — one with no bundle_id creates its
    own, as a draft. So "Book this" from a vendor's page behaves like everything
    else: it assembles something, and the vendor hears about it when it's sent."""
    dispatch = mocker.patch("app.services.booking_service._dispatch_status_notification")
    res = _book_into(world, None)

    assert dispatch.call_count == 0
    assert res["bundle_id"], "a booking always lands in a plan"

    db = TestingSessionLocal()
    made = db.query(Bundle).filter(Bundle.bundle_id == res["bundle_id"]).first()
    assert made.status == "draft"
    db.close()


def test_sending_catches_up_everything_added_meanwhile(mocker, world):
    """Nothing is lost by staying quiet: send notifies every booking still
    pending, including the ones added or swapped in after the plan was built."""
    mocker.patch("app.services.booking_service._dispatch_status_notification")
    bundle_id = _bundle(world["user_id"], "draft")
    booking_id = _book_into(world, bundle_id)["booking_id"]

    dispatch = mocker.patch("app.services.booking_service._dispatch_status_notification")
    db = TestingSessionLocal()
    try:
        select_bundle(bundle_id=bundle_id, caller_user_id=world["user_id"], db=db)
    finally:
        db.close()

    notified = {c.args[1].booking_id for c in dispatch.call_args_list}
    assert booking_id in notified


def test_a_swap_inside_a_draft_tells_nobody(mocker, world):
    """Book the replacement, remove the original — the client sees one action,
    and the vendor should see none of it until the plan is sent."""
    dispatch = mocker.patch("app.services.booking_service._dispatch_status_notification")
    bundle_id = _bundle(world["user_id"], "draft")

    first = _book_into(world, bundle_id)["booking_id"]
    db = TestingSessionLocal()
    db.query(Booking).filter(Booking.booking_id == first).delete()
    db.commit()
    db.close()
    _book_into(world, bundle_id)

    assert dispatch.call_count == 0
