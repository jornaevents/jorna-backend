"""The half-hour warning, and checking in from the link in it.

Two things are worth pinning here. The window — a sweep runs every five minutes
and passes through it repeatedly, so "who is due" has to mean "and hasn't been
told". And the token — it arrives in an inbox, so what it can and can't do is
the whole security story.
"""

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.config import ALGORITHM, SECRET_KEY
from app.db.models import Booking, Bundle, Event, Service, User, Vendor
from app.services.reminder_service import (
    REMINDER_GRACE,
    REMINDER_LEAD,
    ReminderError,
    booking_for_token,
    checkin_token,
    due_for_reminder,
    send_checkin_reminder,
    send_due_reminders,
    starts_at,
)
from tests.test_api import TestingSessionLocal, client

# Evanston, and the reception is at six in the evening. Chicago is UTC-5 in
# June, so six o'clock local is 23:00 UTC — the number every time below is
# measured against.
VENUE = "12 Maple Ave, Evanston, IL 60201"
VENUE_LAT, VENUE_LNG = 42.0451, -87.6877
START_UTC = datetime(2027, 6, 5, 23, 0, tzinfo=timezone.utc)


@pytest.fixture
def booking_world():
    """A paid booking with a vendor who has an email address."""
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]
    client_user = User(
        email=f"r_c_{uid}@t.com", username=f"r_c_{uid}", password="pw", phone="1",
        f_name="Client", l_name="One", age=30, location="Chicago, IL", gender="F",
        language="EN", token_version=0,
    )
    vendor_user = User(
        email=f"r_v_{uid}@t.com", username=f"r_v_{uid}", password="pw", phone="1",
        f_name="Priya", l_name="Vendor", age=30, location="Chicago, IL", gender="F",
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

    service = Service(name="Catering", price=5000.0, duration_minutes=300,
                      vendor_id=vendor.vendor_id, experience="5y")
    db.add(service)
    db.commit()
    db.refresh(service)

    now = datetime.now(timezone.utc)
    # The event carries the address the client typed, geocoded. That pin is what
    # check-in is measured from when no venue is booked — see checkin_anchor.
    event = Event(
        user_id=client_user.user_id, name="Sharma Wedding", date_iso="2027-06-05",
        location=VENUE, address_latitude=VENUE_LAT, address_longitude=VENUE_LNG,
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    bundle = Bundle(user_id=client_user.user_id, name="Plan", event_name="Sharma Wedding",
                    event_id=event.event_id, status="confirmed",
                    created_at=now, updated_at=now)
    db.add(bundle)
    db.commit()
    db.refresh(bundle)

    booking = Booking(
        user_id=client_user.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id, bundle_id=bundle.bundle_id,
        date_iso="2027-06-05", time_start="18:00", time_end="23:00",
        location=VENUE, venue_latitude=VENUE_LAT, venue_longitude=VENUE_LNG,
        status="payment_confirmed", payment_status="paid",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    out = {
        "booking_id": booking.booking_id,
        "vendor_user_id": vendor_user.user_id,
        "vendor_email": vendor_user.email,
        "client_user_id": client_user.user_id,
    }
    db.close()
    yield out


def _db():
    return TestingSessionLocal()


def _booking(db, world):
    return db.query(Booking).filter(Booking.booking_id == world["booking_id"]).first()


def _ids(bookings):
    return {b.booking_id for b in bookings}


# ── When the email is due ─────────────────────────────────────────────


def test_the_start_is_read_in_the_venues_time_not_the_servers(booking_world):
    """Six in the evening in Evanston is 23:00 UTC. Read the stored "18:00" as
    UTC instead and every reminder goes out six hours early."""
    db = _db()
    try:
        assert starts_at(_booking(db, booking_world), db) == START_UTC
    finally:
        db.close()


@pytest.mark.parametrize(
    "minutes_before,due",
    [
        (90, False),   # too early to be useful
        (31, False),   # a minute outside
        (30, True),    # the moment itself
        (15, True),
        (0, True),     # bang on the start
    ],
)
def test_the_window_opens_half_an_hour_out(booking_world, minutes_before, due):
    db = _db()
    try:
        now = START_UTC - timedelta(minutes=minutes_before)
        assert (booking_world["booking_id"] in _ids(due_for_reminder(now=now, db=db))) is due
    finally:
        db.close()


def test_a_sweep_that_ran_late_still_sends(booking_world):
    """Deploys and restarts happen. A booking whose moment passed during the gap
    is still worth telling somebody about — up to a point."""
    db = _db()
    try:
        late = START_UTC + REMINDER_GRACE - timedelta(minutes=1)
        assert booking_world["booking_id"] in _ids(due_for_reminder(now=late, db=db))

        long_gone = START_UTC + REMINDER_GRACE + timedelta(minutes=1)
        assert booking_world["booking_id"] not in _ids(due_for_reminder(now=long_gone, db=db))
    finally:
        db.close()


def test_a_vendor_is_told_once(booking_world, mocker):
    """The sweep runs every five minutes and the window is half an hour wide.
    Without the marker that's six identical emails."""
    sender = mocker.patch(
        "app.services.reminder_service.send_email", return_value={"success": True, "id": "e1"}
    )

    def mine():
        # Counted by recipient, not in total: every test in this file leaves a
        # booking on the same evening behind it, and they are all due too.
        return [
            c for c in sender.call_args_list
            if c.kwargs.get("to") == booking_world["vendor_email"]
        ]

    db = _db()
    try:
        send_due_reminders(db=db, now=START_UTC - REMINDER_LEAD)
        assert len(mine()) == 1

        for minutes in (25, 20, 15, 10, 5, 0):
            send_due_reminders(db=db, now=START_UTC - timedelta(minutes=minutes))
        assert len(mine()) == 1, "told once, not once a sweep"
    finally:
        db.close()


def test_a_vendor_who_never_agreed_is_not_told_to_turn_up(booking_world):
    db = _db()
    try:
        booking = _booking(db, booking_world)
        booking.status = "pending"
        db.commit()
        now = START_UTC - REMINDER_LEAD
        assert booking_world["booking_id"] not in _ids(due_for_reminder(now=now, db=db))
    finally:
        db.close()


def test_a_vendor_already_at_the_venue_is_not_told_to_go(booking_world):
    db = _db()
    try:
        booking = _booking(db, booking_world)
        booking.vendor_checked_in_at = datetime.now(timezone.utc).isoformat()
        db.commit()
        now = START_UTC - REMINDER_LEAD
        assert booking_world["booking_id"] not in _ids(due_for_reminder(now=now, db=db))
    finally:
        db.close()


def test_a_booking_whose_timezone_cannot_be_told_is_skipped(booking_world):
    """Better a missing reminder than one at three in the morning."""
    db = _db()
    try:
        booking = _booking(db, booking_world)
        booking.location = "Somewhere"
        booking.venue_latitude = None
        booking.venue_longitude = None
        db.commit()
        assert starts_at(_booking(db, booking_world), db) is None
        for offset in (60, 30, 0):
            now = START_UTC - timedelta(minutes=offset)
            assert booking_world["booking_id"] not in _ids(due_for_reminder(now=now, db=db))
    finally:
        db.close()


def test_a_failed_send_is_not_retried_into_a_flood(booking_world, mocker):
    sender = mocker.patch(
        "app.services.reminder_service.send_email",
        return_value={"success": False, "error": "bounced"},
    )
    def mine():
        return [
            c for c in sender.call_args_list
            if c.kwargs.get("to") == booking_world["vendor_email"]
        ]

    db = _db()
    try:
        send_due_reminders(db=db, now=START_UTC - REMINDER_LEAD)
        assert len(mine()) == 1
        send_due_reminders(db=db, now=START_UTC - timedelta(minutes=20))
        assert len(mine()) == 1, "a bad address is not retried every five minutes"
    finally:
        db.close()


def test_the_email_says_the_local_time_and_carries_a_link(booking_world, mocker):
    sender = mocker.patch(
        "app.services.reminder_service.send_email", return_value={"success": True, "id": "e1"}
    )
    db = _db()
    try:
        send_checkin_reminder(_booking(db, booking_world), db, now=START_UTC - REMINDER_LEAD)
    finally:
        db.close()

    kwargs = sender.call_args.kwargs
    assert kwargs["to"] == booking_world["vendor_email"]
    body = kwargs["html"] + kwargs["text"]
    assert "6:00 PM" in body, "the venue's clock, not UTC"
    assert "23:00" not in body
    assert "/check-in/?t=" in body
    assert "Sharma Wedding" in body
    assert "Catering" in body


# ── The token ─────────────────────────────────────────────────────────


def test_the_token_names_one_booking(booking_world):
    db = _db()
    try:
        token = checkin_token(_booking(db, booking_world))
        assert booking_for_token(token, db).booking_id == booking_world["booking_id"]
    finally:
        db.close()


def test_an_expired_link_is_refused(booking_world):
    db = _db()
    try:
        stale = checkin_token(
            _booking(db, booking_world), now=datetime.now(timezone.utc) - timedelta(days=3)
        )
        with pytest.raises(ReminderError) as e:
            booking_for_token(stale, db)
        assert e.value.status_code == 401
    finally:
        db.close()


def test_a_login_token_cannot_be_used_to_check_in(booking_world):
    """The purpose claim. Without it, any token this app signs — including a
    vendor's own access token — would be accepted as a check-in link for
    whatever booking id happened to be in its subject."""
    login_shaped = jwt.encode(
        {
            "sub": booking_world["booking_id"],
            "email": "someone@example.com",
            "tv": 0,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    db = _db()
    try:
        with pytest.raises(ReminderError) as e:
            booking_for_token(login_shaped, db)
        assert e.value.status_code == 401
    finally:
        db.close()


def test_a_check_in_token_is_not_a_login(booking_world):
    """The other direction. It must not open anything an account opens."""
    db = _db()
    try:
        token = checkin_token(_booking(db, booking_world))
    finally:
        db.close()

    r = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_a_forged_token_is_refused(booking_world):
    forged = jwt.encode(
        {
            "sub": booking_world["booking_id"],
            "purpose": "checkin",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        "not-the-secret",
        algorithm=ALGORITHM,
    )
    db = _db()
    try:
        with pytest.raises(ReminderError) as e:
            booking_for_token(forged, db)
        assert e.value.status_code == 401
    finally:
        db.close()


# ── Checking in with it ───────────────────────────────────────────────


def test_the_link_still_has_to_be_at_the_venue(booking_world):
    """The token comes out of an inbox, so it is deliberately not sufficient.
    Whoever holds it has to be standing at the venue."""
    db = _db()
    try:
        token = checkin_token(_booking(db, booking_world))
    finally:
        db.close()

    far = client.post(
        f"/checkin/{token}", json={"latitude": 40.7128, "longitude": -74.0060}
    )
    assert far.status_code == 400
    assert "at the venue" in far.json()["detail"]

    db = _db()
    try:
        assert _booking(db, booking_world).vendor_checked_in_at is None
    finally:
        db.close()


def test_checking_in_from_the_link_records_the_vendors_arrival(booking_world):
    db = _db()
    try:
        token = checkin_token(_booking(db, booking_world))
    finally:
        db.close()

    r = client.post(
        f"/checkin/{token}", json={"latitude": VENUE_LAT, "longitude": VENUE_LNG}
    )
    assert r.status_code == 200, r.text

    db = _db()
    try:
        booking = _booking(db, booking_world)
        assert booking.vendor_checked_in_at is not None
        # The vendor's check-in is their half of the escrow release — the same
        # meaning it has in the app, because the same function did it.
        assert booking.vendor_confirmed_at is not None
    finally:
        db.close()


def test_the_link_shows_the_job_and_not_the_plan(booking_world):
    """An inbox is not an account. It says which booking and where, so a vendor
    knows what they're confirming — and nothing about the client or the money."""
    db = _db()
    try:
        token = checkin_token(_booking(db, booking_world))
    finally:
        db.close()

    r = client.get(f"/checkin/{token}")
    assert r.status_code == 200
    body = r.json()
    assert body["location"] == VENUE
    assert body["already_checked_in"] is False
    text = str(body)
    assert "Client" not in text
    assert "5000" not in text
    assert "price" not in body and "user_id" not in body


def test_a_nonsense_link_is_refused():
    assert client.get("/checkin/not-a-token").status_code == 401
    assert (
        client.post(
            "/checkin/not-a-token", json={"latitude": VENUE_LAT, "longitude": VENUE_LNG}
        ).status_code
        == 401
    )
