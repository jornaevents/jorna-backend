"""Nothing incomplete reaches a vendor, and nothing changes after it does.

The web has greyed out its send button on the first half for as long as the
button has existed — but only its own button. select_bundle accepted anything,
so iOS or a direct call sent regardless. And update_booking allowed any edit
while a booking was still 'pending', which is precisely when it is sitting in a
vendor's inbox being read.

Both halves are tested here against the endpoints rather than the helpers, since
the point of the change is that the rule holds where a client can't reach round
it.
"""

import uuid

import pytest

from app.db.models import Booking, Bundle, Service, User, Vendor
from app.services.booking_service import BookingError, create_booking, update_booking
from app.services.bundle_service import BundleError, select_bundle
from app.services.plan_readiness import (
    booking_gaps,
    is_complete_address,
    locked_fields,
)
from tests.test_api import TestingSessionLocal

ADDR = "12 Maple Ave, Evanston, IL 60201"


@pytest.fixture
def plan():
    """A client, a vendor with a flat-rate and a per-person service, a draft."""
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]
    client_user = User(
        email=f"l_c_{uid}@t.com", username=f"l_c_{uid}", password="pw", phone="1",
        f_name="C", l_name="L", age=30, location="Chicago, IL", gender="M",
        language="EN", token_version=0,
    )
    vendor_user = User(
        email=f"l_v_{uid}@t.com", username=f"l_v_{uid}", password="pw", phone="1",
        f_name="V", l_name="N", age=30, location="Chicago, IL", gender="F",
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

    flat = Service(name="DJ", price=500.0, duration_minutes=180,
                   vendor_id=vendor.vendor_id, experience="5y")
    per_head = Service(name="Catering", price=40.0, duration_minutes=300,
                       vendor_id=vendor.vendor_id, experience="5y",
                       price_unit="person")
    db.add_all([flat, per_head])
    db.commit()
    db.refresh(flat)
    db.refresh(per_head)

    out = {
        "user_id": client_user.user_id,
        "vendor_id": vendor.vendor_id,
        "flat_service_id": flat.service_id,
        "per_head_service_id": per_head.service_id,
    }
    db.close()
    yield out


def _db():
    return TestingSessionLocal()


def _book(plan, db, *, bundle_id=None, service_id=None, **over):
    kwargs = dict(
        user_id=plan["user_id"],
        service_id=service_id or plan["flat_service_id"],
        event_name="Wedding", date_iso="2027-06-05", date_end=None,
        time_start="18:00", time_end="23:00", location=ADDR,
        guest_count=200, venue_latitude=None, venue_longitude=None,
        bundle_id=bundle_id, db=db,
    )
    kwargs.update(over)
    return create_booking(**kwargs)


# ── What counts as an address ─────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,complete",
    [
        ("12 Maple Ave, Evanston, IL 60201", True),
        ("12 Maple Ave, Suite 4, Evanston, IL 60201", True),
        ("12 Maple Ave, Evanston, IL 60201-1234", True),
        # The shapes a vendor can't drive to.
        ("Hall", False),
        ("Community Hall", False),
        ("Evanston, IL 60201", False),      # no street
        ("12 Maple Ave, IL 60201", False),  # no city
        ("12 Maple Ave, Evanston, IL", False),   # no postcode
        ("12 Maple Ave, Evanston, 60201", False),  # no state
        ("12 Maple Ave, Evanston, XX 60201", False),  # not a state
        ("", False),
        (None, False),
    ],
)
def test_what_a_full_address_means(raw, complete):
    assert is_complete_address(raw) is complete


# ── Nothing incomplete goes out ───────────────────────────────────────


def test_a_plan_missing_an_address_will_not_send(plan):
    db = _db()
    try:
        created = _book(plan, db, location="Hall")
        with pytest.raises(BundleError) as e:
            select_bundle(
                bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
            )
        assert e.value.status_code == 400
        assert "full address" in e.value.detail

        # And nobody was told.
        bundle = db.query(Bundle).filter(Bundle.bundle_id == created["bundle_id"]).first()
        assert bundle.status == "draft"
    finally:
        db.close()


def test_a_per_person_booking_without_a_headcount_will_not_send(plan):
    db = _db()
    try:
        created = _book(
            plan, db, service_id=plan["per_head_service_id"], guest_count=None
        )
        with pytest.raises(BundleError) as e:
            select_bundle(
                bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
            )
        assert "guest count" in e.value.detail
    finally:
        db.close()


def test_a_plan_without_a_date_will_not_send(plan):
    db = _db()
    try:
        created = _book(plan, db, date_iso="TBD")
        with pytest.raises(BundleError) as e:
            select_bundle(
                bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
            )
        assert "a date" in e.value.detail
    finally:
        db.close()


def test_a_complete_plan_sends(plan):
    db = _db()
    try:
        created = _book(plan, db)
        select_bundle(
            bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
        )
        bundle = db.query(Bundle).filter(Bundle.bundle_id == created["bundle_id"]).first()
        assert bundle.status == "confirmed"
    finally:
        db.close()


def test_a_dead_booking_does_not_block_the_rest(plan):
    """A rejected request is not something the client still has to answer for."""
    db = _db()
    try:
        first = _book(plan, db)
        second = _book(
            plan, db, bundle_id=first["bundle_id"],
            service_id=plan["per_head_service_id"], guest_count=None,
        )
        booking = db.query(Booking).filter(
            Booking.booking_id == second["booking_id"]
        ).first()
        booking.status = "cancelled"
        db.commit()

        select_bundle(
            bundle_id=first["bundle_id"], caller_user_id=plan["user_id"], db=db
        )
        bundle = db.query(Bundle).filter(Bundle.bundle_id == first["bundle_id"]).first()
        assert bundle.status == "confirmed"
    finally:
        db.close()


def test_adding_an_incomplete_service_to_a_live_plan_is_refused(plan):
    """There is no draft to fix it in: a booking joining a sent plan reaches its
    vendor the moment it exists, and can't be edited afterwards."""
    db = _db()
    try:
        created = _book(plan, db)
        select_bundle(
            bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
        )

        with pytest.raises(BookingError) as e:
            _book(
                plan, db, bundle_id=created["bundle_id"],
                service_id=plan["per_head_service_id"], guest_count=None,
            )
        assert "guest count" in e.value.detail

        # Adding a complete one is fine.
        added = _book(
            plan, db, bundle_id=created["bundle_id"],
            service_id=plan["per_head_service_id"], guest_count=150,
        )
        assert added["booking_id"]
    finally:
        db.close()


def test_an_incomplete_draft_can_still_be_assembled(plan):
    """The gate is on sending, not on thinking. A draft is where a plan is
    allowed to be half-finished."""
    db = _db()
    try:
        created = _book(plan, db, location="Hall", date_iso="TBD")
        added = _book(
            plan, db, bundle_id=created["bundle_id"],
            service_id=plan["per_head_service_id"], guest_count=None, location="Hall",
        )
        assert added["booking_id"]
    finally:
        db.close()


# ── And nothing changes after it does ─────────────────────────────────


def test_a_draft_is_freely_editable(plan):
    db = _db()
    try:
        created = _book(plan, db)
        booking = db.query(Booking).filter(
            Booking.booking_id == created["booking_id"]
        ).first()
        assert locked_fields(booking, db) == []

        update_booking(
            booking_id=created["booking_id"], caller_user_id=plan["user_id"],
            update_data={"date_iso": "2027-07-01", "guest_count": 150}, db=db,
        )
        db.refresh(booking)
        assert booking.date_iso == "2027-07-01"
        assert booking.guest_count == 150
    finally:
        db.close()


def test_once_it_is_out_the_details_are_the_vendors(plan):
    db = _db()
    try:
        created = _book(plan, db)
        select_bundle(
            bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
        )

        for field, value in [
            ("date_iso", "2027-07-01"),
            ("location", "99 Other St, Chicago, IL 60601"),
            ("guest_count", 150),
            ("time_start", "20:00"),
        ]:
            with pytest.raises(BookingError) as e:
                update_booking(
                    booking_id=created["booking_id"], caller_user_id=plan["user_id"],
                    update_data={field: value}, db=db,
                )
            assert e.value.status_code == 400
            assert "already with the vendor" in e.value.detail
    finally:
        db.close()


def test_a_pending_request_is_locked_not_just_an_accepted_one(plan):
    """The case the old status check missed. A pending booking is one being read
    by the vendor right now."""
    db = _db()
    try:
        created = _book(plan, db)
        select_bundle(
            bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
        )
        booking = db.query(Booking).filter(
            Booking.booking_id == created["booking_id"]
        ).first()
        assert booking.status == "pending"

        with pytest.raises(BookingError):
            update_booking(
                booking_id=created["booking_id"], caller_user_id=plan["user_id"],
                update_data={"date_iso": "2027-07-01"}, db=db,
            )
    finally:
        db.close()


def test_an_empty_field_can_still_be_filled(plan):
    """Completing a request isn't changing it — and without this a booking that
    went out short of a headcount could never be paid for."""
    db = _db()
    try:
        created = _book(
            plan, db, service_id=plan["per_head_service_id"], guest_count=None
        )
        # Force it out without the headcount, the way a legacy row reached this
        # state before the send gate existed.
        bundle = db.query(Bundle).filter(Bundle.bundle_id == created["bundle_id"]).first()
        bundle.status = "confirmed"
        db.commit()

        booking = db.query(Booking).filter(
            Booking.booking_id == created["booking_id"]
        ).first()
        assert "guest_count" not in locked_fields(booking, db)
        assert "date_iso" in locked_fields(booking, db)

        update_booking(
            booking_id=created["booking_id"], caller_user_id=plan["user_id"],
            update_data={"guest_count": 180}, db=db,
        )
        db.refresh(booking)
        assert booking.guest_count == 180
        # And the price it was missing now resolves.
        assert booking.amount_cents == 180 * 4000
    finally:
        db.close()


def test_filling_a_gap_locks_it_behind_you(plan):
    db = _db()
    try:
        created = _book(
            plan, db, service_id=plan["per_head_service_id"], guest_count=None
        )
        bundle = db.query(Bundle).filter(Bundle.bundle_id == created["bundle_id"]).first()
        bundle.status = "confirmed"
        db.commit()

        update_booking(
            booking_id=created["booking_id"], caller_user_id=plan["user_id"],
            update_data={"guest_count": 180}, db=db,
        )
        with pytest.raises(BookingError):
            update_booking(
                booking_id=created["booking_id"], caller_user_id=plan["user_id"],
                update_data={"guest_count": 250}, db=db,
            )
    finally:
        db.close()


def test_saving_an_unchanged_value_is_not_a_change(plan):
    """A form that autosaves posts every field it holds. Refusing the ones that
    happen to already match would make the client's own editor impossible."""
    db = _db()
    try:
        created = _book(plan, db)
        select_bundle(
            bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
        )
        update_booking(
            booking_id=created["booking_id"], caller_user_id=plan["user_id"],
            update_data={"date_iso": "2027-06-05", "location": ADDR}, db=db,
        )
    finally:
        db.close()


def test_the_payload_says_which_fields_are_locked(plan):
    """The client draws its read-only state from this, and it comes from the
    same function update_booking enforces."""
    from app.services.bundle_service import get_bundle

    db = _db()
    try:
        created = _book(plan, db)
        draft = get_bundle(
            bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
        )
        assert draft["bookings"][0]["locked_fields"] == []

        select_bundle(
            bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
        )
        sent = get_bundle(
            bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
        )
        locked = sent["bookings"][0]["locked_fields"]
        assert "date_iso" in locked
        assert "location" in locked
        assert "guest_count" in locked
    finally:
        db.close()


# ── Every word a vendor might price by ────────────────────────────────
#
# price_unit is free text with no validation, and the pricer accepts far more
# than the readiness check used to. A caterer priced "per head" therefore sent
# with no guest count and arrived at checkout unpayable — the request out, the
# vendor committed, and nobody able to pay them.


PER_PERSON_WORDS = [
    "person", "Person", "per person", "Per Person", "persons", "PERSON",
    "head", "per head", "Per Head", "guest", "per guest", "plate", "per plate",
    "pax", "per pax",
]


@pytest.mark.parametrize("unit", PER_PERSON_WORDS)
def test_every_per_person_word_demands_a_headcount(plan, unit):
    """The readiness check and the pricer have to agree on one string.

    If the pricer reads it as per person and the gate doesn't, the plan sends
    and the booking can never be charged.
    """
    from app.services.booking_service import _normalize_unit, resolve_total_cents

    db = _db()
    try:
        service = db.query(Service).filter(
            Service.service_id == plan["per_head_service_id"]
        ).first()
        service.price_unit = unit
        db.commit()

        assert _normalize_unit(unit) == "person", "the pricer reads this as per person"

        created = _book(
            plan, db, service_id=plan["per_head_service_id"], guest_count=None
        )
        with pytest.raises(BundleError) as e:
            select_bundle(
                bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
            )
        assert "guest count" in e.value.detail

        # And the reason it matters: without a count there is no total.
        booking = db.query(Booking).filter(
            Booking.booking_id == created["booking_id"]
        ).first()
        assert resolve_total_cents(booking, service) is None
    finally:
        db.close()


@pytest.mark.parametrize("unit", ["hour", "hourly", "per hour", "Per Hourly", "hours"])
def test_every_per_hour_word_demands_a_time_window(plan, unit):
    db = _db()
    try:
        service = db.query(Service).filter(
            Service.service_id == plan["flat_service_id"]
        ).first()
        service.price_unit = unit
        db.commit()

        created = _book(plan, db, time_start="", time_end="")
        with pytest.raises(BundleError) as e:
            select_bundle(
                bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
            )
        assert "start and end time" in e.value.detail
    finally:
        db.close()


@pytest.mark.parametrize("unit", ["event", "per event", "flat", None, "", "widgets"])
def test_a_flat_rate_service_asks_for_no_quantity(plan, unit):
    """Unknown words price flat, and a flat rate needs nothing multiplying it."""
    db = _db()
    try:
        service = db.query(Service).filter(
            Service.service_id == plan["flat_service_id"]
        ).first()
        service.price_unit = unit
        db.commit()

        created = _book(plan, db, guest_count=None)
        select_bundle(
            bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
        )
        bundle = db.query(Bundle).filter(Bundle.bundle_id == created["bundle_id"]).first()
        assert bundle.status == "confirmed"
    finally:
        db.close()


def test_the_gate_and_the_pricer_never_disagree():
    """The property, rather than a list of examples.

    Anything the pricer treats as needing a quantity must be something the gate
    demands that quantity for. These were two separate lists once, and the
    difference between them was a booking nobody could pay for.
    """
    from app.services.booking_service import _normalize_unit
    from app.services.plan_readiness import _price_unit_kind

    words = [
        None, "", "person", "Person", "persons", "per person", "PER PERSON",
        "head", "per head", "guest", "guests", "plate", "plates", "pax",
        "hour", "hours", "hourly", "per hour", "Per Hourly",
        "day", "days", "daily", "per day",
        "event", "per event", "flat", "widgets", "  Per   Person  ",
    ]
    for w in words:
        assert _price_unit_kind(w) == (_normalize_unit(w) or "event"), w


# ── Re-sending is a send too ──────────────────────────────────────────


def test_re_sending_will_not_push_out_an_incomplete_request(plan):
    """select_bundle notifies whoever hasn't answered, so a second press is a
    request going out like any other."""
    db = _db()
    try:
        created = _book(plan, db)
        select_bundle(
            bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
        )

        # A booking that lost its headcount after the fact — the vendor changed
        # what they charge by, which nothing else guards against.
        second = _book(
            plan, db, bundle_id=created["bundle_id"],
            service_id=plan["per_head_service_id"], guest_count=200,
        )
        booking = db.query(Booking).filter(
            Booking.booking_id == second["booking_id"]
        ).first()
        booking.guest_count = None
        db.commit()

        with pytest.raises(BundleError) as e:
            select_bundle(
                bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
            )
        assert "guest count" in e.value.detail
    finally:
        db.close()


def test_re_sending_is_not_blocked_by_a_booking_already_answered(plan):
    """An accepted booking isn't being asked again, so its details aren't this
    send's business."""
    db = _db()
    try:
        created = _book(plan, db)
        select_bundle(
            bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
        )
        booking = db.query(Booking).filter(
            Booking.booking_id == created["booking_id"]
        ).first()
        booking.status = "approved"
        booking.guest_count = None
        booking.location = "Hall"
        db.commit()

        # Nothing pending, nothing to refuse.
        select_bundle(
            bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
        )
    finally:
        db.close()
