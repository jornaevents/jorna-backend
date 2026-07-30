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


# ── A draft is invisible, not merely unannounced ──────────────────────
#
# create_booking has always withheld the notification for a booking on a draft
# plan. But the row is created "pending" straight away, and the vendor's
# dashboard, their "Needs you" badge and their bookings page are all built from
# get_vendor_bookings — which returned it. So a service a client added from the
# marketplace while still assembling their plan landed in the vendor's Requests
# queue with Accept and Decline on it. Silence is not absence.


class TestDraftBookingsAreInvisibleToVendors:
    def _setup(self, db, bundle_status="draft"):
        import uuid
        from datetime import datetime, timezone
        from app.db.models import Booking, Bundle, Service, User, Vendor

        uid = uuid.uuid4().hex[:8]
        client = User(
            email=f"dv_c_{uid}@test.com", username=f"dv_c_{uid}", password="pw",
            phone="1", f_name="Cli", l_name="Ent", age=30, location="NJ",
            gender="F", language="EN", token_version=0,
        )
        vu = User(
            email=f"dv_v_{uid}@test.com", username=f"dv_v_{uid}", password="pw",
            phone="1", f_name="Ven", l_name="Dor", age=30, location="NJ",
            gender="F", language="EN", token_version=0,
        )
        db.add_all([client, vu]); db.commit(); db.refresh(client); db.refresh(vu)
        v = Vendor(user_id=vu.user_id, bio="b", category="venue", rating=4.0, num_events=1)
        db.add(v); db.commit(); db.refresh(v)
        svc = Service(name=f"svc-{uid}", price=100.0, vendor_id=v.vendor_id,
                      experience="e", category="venue", negotiable=False)
        db.add(svc); db.commit(); db.refresh(svc)
        now = datetime.now(timezone.utc)
        bundle = Bundle(user_id=client.user_id, name="Plan", status=bundle_status,
                        created_at=now, updated_at=now)
        db.add(bundle); db.commit(); db.refresh(bundle)
        bk = Booking(
            user_id=client.user_id, vendor_id=v.vendor_id, service_id=svc.service_id,
            date_iso="2027-06-05", time_start="18:00", time_end="23:00",
            location="NJ", status="pending", bundle_id=bundle.bundle_id,
        )
        db.add(bk); db.commit(); db.refresh(bk)
        return {"client": client, "vendor_user": vu, "vendor": v,
                "service": svc, "bundle": bundle, "booking": bk}

    def _teardown(self, db, s):
        from app.db.models import Booking, Bundle, Service, User, Vendor

        db.query(Booking).filter(Booking.booking_id == s["booking"].booking_id).delete()
        db.query(Bundle).filter(Bundle.bundle_id == s["bundle"].bundle_id).delete()
        db.query(Service).filter(Service.service_id == s["service"].service_id).delete()
        db.query(Vendor).filter(Vendor.vendor_id == s["vendor"].vendor_id).delete()
        db.query(User).filter(
            User.user_id.in_([s["client"].user_id, s["vendor_user"].user_id])
        ).delete(synchronize_session=False)
        db.commit()

    def test_a_draft_booking_is_not_in_the_vendors_list(self):
        from app.services.booking_service import get_vendor_bookings
        from tests.test_api import TestingSessionLocal

        db = TestingSessionLocal()
        s = self._setup(db)
        try:
            res = get_vendor_bookings(
                vendor_id=s["vendor"].vendor_id,
                caller_user_id=s["vendor_user"].user_id, limit=100, db=db,
            )
            assert res["total"] == 0, "a plan nobody sent reached the vendor's queue"
            assert res["items"] == []
        finally:
            self._teardown(db, s); db.close()

    def test_sending_the_plan_makes_it_appear(self):
        """The catch-up select_bundle already performs, seen from the vendor."""
        from app.services.booking_service import get_vendor_bookings
        from tests.test_api import TestingSessionLocal

        db = TestingSessionLocal()
        s = self._setup(db)
        try:
            s["bundle"].status = "confirmed"
            db.commit()
            res = get_vendor_bookings(
                vendor_id=s["vendor"].vendor_id,
                caller_user_id=s["vendor_user"].user_id, limit=100, db=db,
            )
            assert res["total"] == 1
            assert res["items"][0]["booking_id"] == s["booking"].booking_id
        finally:
            self._teardown(db, s); db.close()

    def test_a_vendor_cannot_accept_a_draft_booking(self):
        """The backstop. Agreeing to a draft means agreeing to a date and a
        place the client hasn't decided on yet."""
        from app.models.schemas import BookingStatus
        from app.services.booking_service import BookingError, update_booking_status
        from tests.test_api import TestingSessionLocal

        db = TestingSessionLocal()
        s = self._setup(db)
        try:
            with pytest.raises(BookingError) as caught:
                update_booking_status(
                    booking_id=s["booking"].booking_id,
                    caller_user_id=s["vendor_user"].user_id,
                    status=BookingStatus.APPROVED, db=db,
                )
            assert caught.value.status_code == 400
            assert "hasn't been sent" in caught.value.detail
            db.refresh(s["booking"])
            assert s["booking"].status == "pending"
        finally:
            self._teardown(db, s); db.close()

    def test_a_booking_with_no_bundle_still_shows(self):
        """Nothing is hiding it, and legacy rows predate bundles being required."""
        from app.services.booking_service import get_vendor_bookings
        from tests.test_api import TestingSessionLocal

        db = TestingSessionLocal()
        s = self._setup(db)
        try:
            s["booking"].bundle_id = None
            db.commit()
            res = get_vendor_bookings(
                vendor_id=s["vendor"].vendor_id,
                caller_user_id=s["vendor_user"].user_id, limit=100, db=db,
            )
            assert res["total"] == 1
        finally:
            self._teardown(db, s); db.close()
