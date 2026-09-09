"""A service can be put in a plan before the plan is worked out.

Assembling a celebration is not a linear form. A client finds a DJ they like in
week one and does not yet know the date, the hall, or how many people are
coming; asking for all of it on every service they add means answering the same
four questions five times and inventing answers to the ones they don't have.

So a booking can join a *draft* plan with its details blank. Nothing is sent, no
vendor is told, and the plan's "still needed" card is what chases the gaps
later. Pressing Send is what tells everyone, and by then the plan has to be
complete — see test_locked_after_send.

The backend already behaved this way; nothing tested it. The booking form now
offers it as a button, so the behaviour is load-bearing and pinned here.
"""
import uuid
from datetime import date, timedelta

from app.db.models import Booking, Bundle, Service, User, Vendor
from app.services.booking_service import BookingError, create_booking
from tests.test_api import TestingSessionLocal


def _db():
    return TestingSessionLocal()


def _seed(price_unit="person", price=40.0):
    db = _db()
    uid = uuid.uuid4().hex[:8]

    client_user = User(
        email=f"draft_c_{uid}@t.com", username=f"draft_c_{uid}", password="pw",
        phone="1", f_name="Client", l_name="User", age=30, location="NJ",
        gender="F", language="EN", token_version=0,
    )
    vendor_user = User(
        email=f"draft_v_{uid}@t.com", username=f"draft_v_{uid}", password="pw",
        phone="1", f_name="Vendor", l_name="User", age=35, location="NJ",
        gender="M", language="EN", token_version=0,
    )
    db.add_all([client_user, vendor_user]); db.commit()
    db.refresh(client_user); db.refresh(vendor_user)

    vendor = Vendor(user_id=vendor_user.user_id, bio="b", rating=0.0, num_events=0)
    db.add(vendor); db.commit(); db.refresh(vendor)

    service = Service(name="Wedding DJ", price=price, vendor_id=vendor.vendor_id,
                      experience="8y", price_unit=price_unit)
    db.add(service); db.commit(); db.refresh(service)

    out = {"user_id": client_user.user_id, "service_id": service.service_id,
           "vendor_id": vendor.vendor_id}
    db.close()
    return out


def _blank_booking(seeded, db, bundle_id=None, **overrides):
    """What the form sends when someone presses Add as draft having typed nothing.

    Times carry the form's 17:00–23:00 default; everything else is empty. Empty
    strings rather than nulls because the columns are NOT NULL — blank is how
    this codebase has always spelled "not said yet", and what booking_gaps reads.
    """
    kwargs = dict(
        user_id=seeded["user_id"],
        service_id=seeded["service_id"],
        event_name="My Event",
        date_iso="",
        date_end=None,
        time_start="17:00",
        time_end="23:00",
        location="",
        guest_count=None,
        venue_latitude=None,
        venue_longitude=None,
        bundle_id=bundle_id,
        db=db,
    )
    kwargs.update(overrides)
    return create_booking(**kwargs)


def test_a_service_joins_a_new_plan_with_nothing_filled_in():
    """The whole point. No date, no address, no headcount — and it's accepted."""
    seeded = _seed()
    db = _db()
    try:
        created = _blank_booking(seeded, db)

        booking = db.query(Booking).filter(
            Booking.booking_id == created["booking_id"]
        ).first()
        assert booking is not None
        assert booking.date_iso == ""
        assert booking.location == ""
        assert booking.guest_count is None
    finally:
        db.close()


def test_a_new_plan_starts_as_a_draft():
    """Which is what keeps the vendor from being told."""
    seeded = _seed()
    db = _db()
    try:
        created = _blank_booking(seeded, db)
        bundle = db.query(Bundle).filter(
            Bundle.bundle_id == created["bundle_id"]
        ).first()
        assert bundle.status == "draft"
    finally:
        db.close()


def test_nobody_is_told_about_a_draft():
    """A vendor asked to hold a date for a plan with no date, that may be undone
    a minute later, is being asked for nothing useful."""
    seeded = _seed()
    db = _db()
    try:
        created = _blank_booking(seeded, db)
        assert created["notification"] == {
            "skipped": "Draft plan — vendors are told when it's sent."
        }
    finally:
        db.close()


def test_a_per_person_service_can_be_drafted_without_a_headcount():
    """The gate that refuses to *send* one of these doesn't refuse to hold it.
    Those are different moments and only the second is a promise to anyone."""
    seeded = _seed(price_unit="person")
    db = _db()
    try:
        created = _blank_booking(seeded, db)
        booking = db.query(Booking).filter(
            Booking.booking_id == created["booking_id"]
        ).first()
        # No quantity, so no total is claimed — rather than the bare $40 rate
        # standing in for one, which is what makes an unpayable booking.
        assert booking.guest_count is None
        assert booking.amount_cents is None
    finally:
        db.close()


def test_several_services_can_be_drafted_into_one_plan():
    """Assembling means adding the next thing."""
    seeded = _seed()
    db = _db()
    try:
        first = _blank_booking(seeded, db)
        bundle_id = first["bundle_id"]

        second_service = Service(name="Mandap Design", price=3000.0,
                                 vendor_id=seeded["vendor_id"], experience="8y",
                                 price_unit="event")
        db.add(second_service); db.commit(); db.refresh(second_service)

        _blank_booking(seeded, db, bundle_id=bundle_id,
                       service_id=second_service.service_id)

        count = db.query(Booking).filter(Booking.bundle_id == bundle_id).count()
        assert count == 2
    finally:
        db.close()


def test_a_draft_can_be_completed_afterwards():
    """The round trip the dashboard's card exists to close: added blank, filled
    in later, and then it has everything sending requires."""
    from app.services.booking_service import update_booking
    from app.services.plan_readiness import booking_gaps

    seeded = _seed(price_unit="person")
    db = _db()
    try:
        created = _blank_booking(seeded, db)
        booking = db.query(Booking).filter(
            Booking.booking_id == created["booking_id"]
        ).first()
        service = db.query(Service).filter(
            Service.service_id == seeded["service_id"]
        ).first()

        assert booking_gaps(booking, service), "a blank draft should read as incomplete"

        update_booking(
            caller_user_id=seeded["user_id"],
            booking_id=booking.booking_id,
            update_data={
                # Relative to today rather than a fixed date — this test only
                # cares that filling in every gap clears it, not what the date
                # actually is, and a hardcoded one goes stale as soon as it's
                # in the past (which is exactly the gap this fix now catches).
                "date_iso": (date.today() + timedelta(days=180)).isoformat(),
                "location": "12 Maple Ave, Evanston, IL 60201",
                "guest_count": 220,
            },
            db=db,
        )

        db.refresh(booking)
        assert booking_gaps(booking, service) == []
    finally:
        db.close()


def test_a_plan_already_with_its_vendors_refuses_a_blank_addition():
    """The other side of the same rule, and the reason the button is hidden for
    a sent plan: there is no draft left to fix it in, and the request goes out
    the moment the row exists."""
    seeded = _seed(price_unit="person")
    db = _db()
    try:
        created = _blank_booking(seeded, db)
        bundle = db.query(Bundle).filter(
            Bundle.bundle_id == created["bundle_id"]
        ).first()
        bundle.status = "confirmed"
        db.commit()

        second_service = Service(name="Catering", price=40.0,
                                 vendor_id=seeded["vendor_id"], experience="8y",
                                 price_unit="person")
        db.add(second_service); db.commit(); db.refresh(second_service)

        try:
            _blank_booking(seeded, db, bundle_id=bundle.bundle_id,
                           service_id=second_service.service_id)
            raise AssertionError("expected a blank addition to a sent plan to be refused")
        except BookingError as e:
            assert e.status_code == 400
            assert "already with your vendors" in e.detail
    finally:
        db.close()
