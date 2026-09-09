from datetime import date, timedelta

import pytest
from app.db.models import Booking, User, Service, Vendor
from app.limiter import limiter
from tests.test_api import TestingSessionLocal, client, make_auth_headers
import uuid


def _future_date(offset_days: int = 180) -> str:
    """A booking date guaranteed ahead of "today" — update_booking/create_booking
    now refuse a past one (see is_in_the_past), and a hardcoded literal here
    is exactly the fixture that already drifted into the past once before
    (test_a_draft_can_be_completed_afterwards, jorna-backend #34)."""
    return (date.today() + timedelta(days=offset_days)).isoformat()


@pytest.fixture(autouse=True)
def without_rate_limits():
    """POST /bookings allows 10/minute, shared across the whole suite's one
    client address — this file alone now posts to it more than that. Same
    fix as test_google_register.py's without_rate_limits, for the same
    reason: left on, these tests fail based on what ran before them."""
    limiter.enabled = False
    yield
    limiter.enabled = True


@pytest.fixture
def seeded_db():
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    user = User(
        email=f"test_bookings_{uid}@test.com",
        username=f"test_bookings_{uid}",
        password="pw", phone="1", f_name="A", l_name="B",
        age=20, location="123", gender="M", language="EN", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    vendor_user = User(
        email=f"test_vendor_{uid}@test.com",
        username=f"test_vendor_{uid}",
        password="pw", phone="1", f_name="V", l_name="U",
        age=30, location="456", gender="M", language="EN", token_version=0,
    )
    db.add(vendor_user)
    db.commit()
    db.refresh(vendor_user)

    vendor = Vendor(user_id=vendor_user.user_id, bio="bio", rating=5.0, num_events=1)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    service = Service(name="Test Service", price=100.0, duration_minutes=60, vendor_id=vendor.vendor_id, experience="none")
    db.add(service)
    db.commit()
    db.refresh(service)

    yield {"user": user, "vendor_user": vendor_user, "vendor": vendor, "service": service, "db": db}
    db.close()


def test_create_booking(seeded_db):
    user = seeded_db["user"]
    service = seeded_db["service"]
    headers = make_auth_headers(user)

    response = client.post(
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
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "pending"
    assert "booking_id" in data


def test_create_booking_rejects_non_positive_guest_count(seeded_db):
    user = seeded_db["user"]
    service = seeded_db["service"]
    headers = make_auth_headers(user)

    for bad_count in (-5, 0):
        response = client.post(
            "/bookings",
            json={
                "service_id": service.service_id,
                "event_name": "Test Event",
                "time_start": "13:00",
                "time_end": "15:00",
                "location": "123 Test St",
                "date_iso": _future_date(180),
                "guest_count": bad_count,
                "venue_latitude": 34.0,
                "venue_longitude": -118.0,
            },
            headers=headers,
        )
        assert response.status_code == 422, bad_count


def test_update_booking_rejects_non_positive_guest_count(seeded_db):
    user = seeded_db["user"]
    service = seeded_db["service"]
    headers = make_auth_headers(user)

    create = client.post(
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
        headers=headers,
    )
    booking_id = create.json()["booking_id"]

    response = client.patch(
        f"/bookings/{booking_id}",
        json={"guest_count": -5},
        headers=headers,
    )
    assert response.status_code == 422


def test_create_booking_rejects_non_positive_performer_count(seeded_db):
    user = seeded_db["user"]
    service = seeded_db["service"]
    headers = make_auth_headers(user)

    for bad_count in (-5, 0):
        response = client.post(
            "/bookings",
            json={
                "service_id": service.service_id,
                "event_name": "Test Event",
                "time_start": "13:00",
                "time_end": "15:00",
                "location": "123 Test St",
                "date_iso": _future_date(180),
                "performer_count": bad_count,
                "venue_latitude": 34.0,
                "venue_longitude": -118.0,
            },
            headers=headers,
        )
        assert response.status_code == 422, bad_count


def test_update_booking_rejects_non_positive_performer_count(seeded_db):
    user = seeded_db["user"]
    service = seeded_db["service"]
    headers = make_auth_headers(user)

    create = client.post(
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
        headers=headers,
    )
    booking_id = create.json()["booking_id"]

    response = client.patch(
        f"/bookings/{booking_id}",
        json={"performer_count": -5},
        headers=headers,
    )
    assert response.status_code == 422


def test_client_note_round_trips_to_client_and_vendor(seeded_db):
    """What the client writes in 'Anything the vendor should know?' must be
    stored and read back identically by both parties — this is the field the
    vendor actually needs to see, not just the client who wrote it."""
    user = seeded_db["user"]
    vendor_user = seeded_db["vendor_user"]
    service = seeded_db["service"]
    client_headers = make_auth_headers(user)
    vendor_headers = make_auth_headers(vendor_user)
    note = "Please arrive 30 minutes early, there's limited parking."

    create = client.post(
        "/bookings",
        json={
            "service_id": service.service_id,
            "event_name": "Note Round Trip Event",
            "time_start": "13:00",
            "time_end": "15:00",
            "location": "123 Test St",
            "date_iso": _future_date(181),
            "client_note": note,
        },
        headers=client_headers,
    )
    assert create.status_code == 200
    booking_id = create.json()["booking_id"]

    as_client = client.get(f"/bookings/{booking_id}", headers=client_headers)
    assert as_client.status_code == 200
    assert as_client.json()["client_note"] == note

    as_vendor = client.get(f"/bookings/{booking_id}", headers=vendor_headers)
    assert as_vendor.status_code == 200
    assert as_vendor.json()["client_note"] == note


def test_client_note_defaults_to_null(seeded_db):
    """A booking made without a note must not fail or fabricate one."""
    user = seeded_db["user"]
    service = seeded_db["service"]
    headers = make_auth_headers(user)

    create = client.post(
        "/bookings",
        json={
            "service_id": service.service_id,
            "event_name": "No Note Event",
            "time_start": "10:00",
            "time_end": "12:00",
            "location": "123 Test St",
            "date_iso": _future_date(182),
        },
        headers=headers,
    )
    assert create.status_code == 200
    booking_id = create.json()["booking_id"]

    fetched = client.get(f"/bookings/{booking_id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["client_note"] is None


def test_duplicate_booking_returns_existing(seeded_db):
    """Retrying the same slot (same bundle+vendor+service+date) must not
    create a second booking — the existing one is returned."""
    user = seeded_db["user"]
    service = seeded_db["service"]
    db = seeded_db["db"]
    headers = make_auth_headers(user)

    payload = {
        "service_id": service.service_id,
        "event_name": "Dupe Guard Event",
        "time_start": "13:00",
        "time_end": "15:00",
        "location": "123 Test St",
        "date_iso": _future_date(210),
    }
    first = client.post("/bookings", json=payload, headers=headers)
    assert first.status_code == 200
    first_data = first.json()

    # Retry into the same auto-created bundle → same booking back, no dupe.
    payload["bundle_id"] = first_data["bundle_id"]
    second = client.post("/bookings", json=payload, headers=headers)
    assert second.status_code == 200
    second_data = second.json()
    assert second_data["booking_id"] == first_data["booking_id"]

    count = db.query(Booking).filter(
        Booking.bundle_id == first_data["bundle_id"],
        Booking.service_id == service.service_id,
        Booking.date_iso == _future_date(210),
    ).count()
    assert count == 1


def test_rebook_allowed_after_rejection(seeded_db):
    """A rejected booking must not block re-booking the same slot."""
    user = seeded_db["user"]
    service = seeded_db["service"]
    db = seeded_db["db"]
    headers = make_auth_headers(user)

    payload = {
        "service_id": service.service_id,
        "event_name": "Rebook Event",
        "time_start": "10:00",
        "time_end": "12:00",
        "location": "123 Test St",
        "date_iso": _future_date(211),
    }
    first = client.post("/bookings", json=payload, headers=headers)
    assert first.status_code == 200
    first_data = first.json()

    # Vendor rejects it.
    booking = db.query(Booking).filter(Booking.booking_id == first_data["booking_id"]).first()
    booking.status = "rejected"
    db.commit()

    # Same slot books again — new booking, not the rejected one.
    payload["bundle_id"] = first_data["bundle_id"]
    second = client.post("/bookings", json=payload, headers=headers)
    assert second.status_code == 200
    assert second.json()["booking_id"] != first_data["booking_id"]


def test_approve_booking(seeded_db):
    db = seeded_db["db"]
    user = seeded_db["user"]
    vendor = seeded_db["vendor"]
    vendor_user = seeded_db["vendor_user"]
    service = seeded_db["service"]

    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="10:00", time_end="11:30",
        location="145 Main St", date_iso=_future_date(120),
        venue_latitude=40.0, venue_longitude=-70.0, status="pending",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    # Vendor approves
    headers = make_auth_headers(vendor_user)
    response = client.put(
        f"/bookings/{booking.booking_id}/status",
        json={"status": "approved"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["message"] == "Booking successfully updated to approved"

    db.refresh(booking)
    assert booking.status == "approved"


def test_approve_skips_the_charge_for_a_manual_track_booking(seeded_db, mocker):
    charge = mocker.patch("app.services.stripe_service.charge_saved_card")
    db = seeded_db["db"]
    user = seeded_db["user"]
    vendor = seeded_db["vendor"]
    vendor_user = seeded_db["vendor_user"]
    service = seeded_db["service"]

    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="10:00", time_end="11:30",
        location="145 Main St", date_iso=_future_date(120),
        venue_latitude=40.0, venue_longitude=-70.0, status="pending",
        payment_method="manual",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    headers = make_auth_headers(vendor_user)
    response = client.put(
        f"/bookings/{booking.booking_id}/status",
        json={"status": "approved"},
        headers=headers,
    )
    assert response.status_code == 200
    charge.assert_not_called()


def test_client_cannot_approve(seeded_db):
    db = seeded_db["db"]
    user = seeded_db["user"]
    vendor = seeded_db["vendor"]
    service = seeded_db["service"]

    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="10:00", time_end="11:30",
        location="145 Main St", date_iso=_future_date(120),
        venue_latitude=40.0, venue_longitude=-70.0, status="pending",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    # Client tries to approve — should be rejected (only vendors can approve)
    headers = make_auth_headers(user)
    response = client.put(
        f"/bookings/{booking.booking_id}/status",
        json={"status": "approved"},
        headers=headers,
    )
    assert response.status_code == 403


def test_get_user_bookings(seeded_db):
    db = seeded_db["db"]
    user = seeded_db["user"]
    vendor = seeded_db["vendor"]
    service = seeded_db["service"]

    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="10:00", time_end="11:30",
        location="145 Main St", date_iso=_future_date(120), status="pending",
    )
    db.add(booking)
    db.commit()

    headers = make_auth_headers(user)
    response = client.get(f"/bookings/user/{user.user_id}", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert len(data["items"]) > 0
    assert "total" in data


def test_get_user_bookings_forbidden(seeded_db):
    """A user cannot fetch another user's bookings."""
    user = seeded_db["user"]
    vendor_user = seeded_db["vendor_user"]

    headers = make_auth_headers(vendor_user)
    response = client.get(f"/bookings/user/{user.user_id}", headers=headers)
    assert response.status_code == 403


def test_get_vendor_bookings(seeded_db):
    db = seeded_db["db"]
    user = seeded_db["user"]
    vendor = seeded_db["vendor"]
    vendor_user = seeded_db["vendor_user"]
    service = seeded_db["service"]

    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="10:00", time_end="11:30",
        location="145 Main St", date_iso=_future_date(120), status="pending",
    )
    db.add(booking)
    db.commit()

    headers = make_auth_headers(vendor_user)
    response = client.get(f"/bookings/vendor/{vendor.vendor_id}", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert len(data["items"]) > 0


def test_get_vendor_bookings_forbidden(seeded_db):
    """A non-vendor user cannot fetch another vendor's bookings."""
    vendor = seeded_db["vendor"]
    user = seeded_db["user"]

    headers = make_auth_headers(user)
    response = client.get(f"/bookings/vendor/{vendor.vendor_id}", headers=headers)
    assert response.status_code == 403


def test_unauthenticated_booking_rejected():
    """Requests without a token should be rejected with 401 or 403."""
    response = client.post(
        "/bookings",
        json={
            "service_id": "any-id",
            "event_name": "Test",
            "time_start": "10:00",
            "time_end": "11:00",
            "location": "Somewhere",
            "date_iso": _future_date(180),
        },
    )
    assert response.status_code in (401, 403)


# ── payment_method snapshot (optional-escrow Phase 1) ──────────────────
#
# Booking.payment_method is stamped from the vendor's current setting at
# creation time, not read live later — so a vendor switching tracks after a
# booking already exists doesn't change the terms of that booking.

def test_create_booking_snapshots_vendors_payment_method(seeded_db):
    from app.db.models import Booking as BookingModel

    user = seeded_db["user"]
    service = seeded_db["service"]
    headers = make_auth_headers(user)

    response = client.post(
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
        headers=headers,
    )
    assert response.status_code == 200, response.text
    booking_id = response.json()["booking_id"]

    db = TestingSessionLocal()
    booking = db.query(BookingModel).filter(BookingModel.booking_id == booking_id).first()
    assert booking.payment_method == "stripe"
    db.close()


def test_create_booking_snapshots_manual_payment_method(seeded_db):
    from app.db.models import Booking as BookingModel, Vendor as VendorModel

    user = seeded_db["user"]
    vendor = seeded_db["vendor"]
    service = seeded_db["service"]
    headers = make_auth_headers(user)

    db = TestingSessionLocal()
    db.query(VendorModel).filter(VendorModel.vendor_id == vendor.vendor_id).update(
        {"payment_method": "manual", "venmo_handle": "@vendor"}
    )
    db.commit()
    db.close()

    response = client.post(
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
        headers=headers,
    )
    assert response.status_code == 200, response.text
    booking_id = response.json()["booking_id"]

    db = TestingSessionLocal()
    booking = db.query(BookingModel).filter(BookingModel.booking_id == booking_id).first()
    assert booking.payment_method == "manual"
    db.close()


# ── A vendor pulling out of a booking they accepted ───────────────────
#
# A vendor whose circumstances changed had no way out of an accepted booking at
# all: the status machine allowed approve/reject only from pending, so the
# honest options were to say so in the chat and hope, or not turn up. Allowed
# now — but only while the money hasn't moved, because past that the client is
# out of pocket for a date they're holding and unwinding it is a refund with its
# own rules, not a status change.


class TestVendorCancellingAnAcceptedBooking:
    def _setup(self, db, payment_status=None, status="approved"):
        import uuid
        from datetime import datetime, timezone
        from app.db.models import Booking, Bundle, Service, User, Vendor

        uid = uuid.uuid4().hex[:8]
        client = User(
            email=f"vc_c_{uid}@test.com", username=f"vc_c_{uid}", password="pw",
            phone="1", f_name="C", l_name="L", age=30, location="NJ",
            gender="F", language="EN", token_version=0,
        )
        vu = User(
            email=f"vc_v_{uid}@test.com", username=f"vc_v_{uid}", password="pw",
            phone="1", f_name="V", l_name="N", age=30, location="NJ",
            gender="F", language="EN", token_version=0,
        )
        db.add_all([client, vu]); db.commit(); db.refresh(client); db.refresh(vu)
        v = Vendor(user_id=vu.user_id, bio="b", category="venue", rating=4.0, num_events=1)
        db.add(v); db.commit(); db.refresh(v)
        svc = Service(name=f"svc-{uid}", price=1000.0, price_unit="event",
                      vendor_id=v.vendor_id, experience="e", category="venue",
                      negotiable=False)
        db.add(svc); db.commit(); db.refresh(svc)
        now = datetime.now(timezone.utc)
        bundle = Bundle(user_id=client.user_id, name="Plan", status="confirmed",
                        created_at=now, updated_at=now)
        db.add(bundle); db.commit(); db.refresh(bundle)
        bk = Booking(
            user_id=client.user_id, vendor_id=v.vendor_id, service_id=svc.service_id,
            date_iso="2027-06-05", time_start="18:00", time_end="23:00",
            location="12 Maple Ave, Evanston, IL 60201", status=status,
            payment_status=payment_status, bundle_id=bundle.bundle_id,
        )
        db.add(bk); db.commit(); db.refresh(bk)
        return {"client": client, "vendor_user": vu, "vendor": v, "service": svc,
                "bundle": bundle, "booking": bk}

    def _teardown(self, db, s):
        from app.db.models import Booking, Bundle, ChangeRequest, Service, User, Vendor

        db.query(ChangeRequest).filter(
            ChangeRequest.booking_id == s["booking"].booking_id
        ).delete(synchronize_session=False)
        db.query(Booking).filter(Booking.booking_id == s["booking"].booking_id).delete()
        db.query(Bundle).filter(Bundle.bundle_id == s["bundle"].bundle_id).delete()
        db.query(Service).filter(Service.service_id == s["service"].service_id).delete()
        db.query(Vendor).filter(Vendor.vendor_id == s["vendor"].vendor_id).delete()
        db.query(User).filter(
            User.user_id.in_([s["client"].user_id, s["vendor_user"].user_id])
        ).delete(synchronize_session=False)
        db.commit()

    def _cancel(self, db, s, caller=None):
        from app.models.schemas import BookingStatus
        from app.services.booking_service import update_booking_status

        return update_booking_status(
            booking_id=s["booking"].booking_id,
            caller_user_id=caller or s["vendor_user"].user_id,
            status=BookingStatus.REJECTED, db=db,
        )

    def test_an_unpaid_accepted_booking_can_be_cancelled(self):
        from tests.test_api import TestingSessionLocal

        db = TestingSessionLocal()
        s = self._setup(db, payment_status="unpaid")
        try:
            self._cancel(db, s)
            db.refresh(s["booking"])
            assert s["booking"].status == "rejected"
        finally:
            self._teardown(db, s); db.close()

    def test_a_never_paid_booking_can_be_cancelled(self):
        """payment_status is null on a booking nobody has been asked to pay."""
        from tests.test_api import TestingSessionLocal

        db = TestingSessionLocal()
        s = self._setup(db, payment_status=None)
        try:
            self._cancel(db, s)
            db.refresh(s["booking"])
            assert s["booking"].status == "rejected"
        finally:
            self._teardown(db, s); db.close()

    @pytest.mark.parametrize(
        "payment_status", ["processing", "released", "refunded", "disputed"]
    )
    def test_money_having_moved_blocks_it(self, payment_status):
        """Including 'processing' — money in flight is the worst moment to walk
        away, not an exempt one. 'paid' is the exception now — see
        test_vendor_cancelling_a_paid_booking_refunds_it below."""
        from app.services.booking_service import BookingError
        from tests.test_api import TestingSessionLocal

        db = TestingSessionLocal()
        s = self._setup(db, payment_status=payment_status)
        try:
            with pytest.raises(BookingError) as caught:
                self._cancel(db, s)
            assert caught.value.status_code == 400
            db.refresh(s["booking"])
            assert s["booking"].status == "approved", "cancelled with money on it"
        finally:
            self._teardown(db, s); db.close()

    def test_vendor_cancelling_a_paid_booking_refunds_it(self, mocker):
        """A vendor backing out of an accepted, paid booking is no longer
        blocked outright — it always triggers a full refund to the client,
        no ramp, since the vendor is the one breaking the commitment."""
        from tests.test_api import TestingSessionLocal

        refund = mocker.patch("stripe.Refund.create")
        db = TestingSessionLocal()
        s = self._setup(db, payment_status="paid")
        s["booking"].payment_intent_id = "pi_vendor_cancel_test"
        db.commit()
        try:
            self._cancel(db, s)
            refund.assert_called_once()
            db.refresh(s["booking"])
            assert s["booking"].status == "rejected"
            assert s["booking"].payment_status == "refunded"
        finally:
            self._teardown(db, s); db.close()

    def test_the_client_cannot_cancel_this_way(self):
        """Rejecting is the vendor's verb. A client withdraws instead."""
        from app.services.booking_service import BookingError
        from tests.test_api import TestingSessionLocal

        db = TestingSessionLocal()
        s = self._setup(db, payment_status="unpaid")
        try:
            with pytest.raises(BookingError) as caught:
                self._cancel(db, s, caller=s["client"].user_id)
            assert caught.value.status_code == 403
        finally:
            self._teardown(db, s); db.close()

    def test_cancelling_closes_an_open_date_change(self):
        """Nothing is waiting on a booking that isn't happening."""
        from datetime import datetime, timezone
        from app.db.models import ChangeRequest
        from tests.test_api import TestingSessionLocal

        db = TestingSessionLocal()
        s = self._setup(db, payment_status="unpaid")
        cr = ChangeRequest(
            booking_id=s["booking"].booking_id, proposed_by=s["client"].user_id,
            status="pending", date_iso="2027-07-01",
            created_at=datetime.now(timezone.utc),
        )
        db.add(cr); db.commit()
        try:
            self._cancel(db, s)
            db.refresh(cr)
            assert cr.status == "withdrawn"
        finally:
            self._teardown(db, s); db.close()

    def test_a_payment_landing_on_a_cancelled_booking_is_refunded(self, mocker):
        """The checkout race. create_checkout_session leaves payment_status
        alone, so a client on Stripe's page still reads as unpaid — a vendor
        cancelling in that window passes the check honestly, and then the
        webhook arrives."""
        from app.services.stripe_service import _mark_booking_paid
        from tests.test_api import TestingSessionLocal

        refund = mocker.patch("stripe.Refund.create")
        db = TestingSessionLocal()
        s = self._setup(db, payment_status="unpaid", status="rejected")
        try:
            moved = _mark_booking_paid(s["booking"], "pi_race_123", db)
            assert moved is False, "a cancelled booking was marked paid"
            refund.assert_called_once()
            db.refresh(s["booking"])
            assert s["booking"].payment_status == "refunded"
            assert s["booking"].status == "rejected"
        finally:
            self._teardown(db, s); db.close()
