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

import pytest
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
    per_performer = Service(name="Dance Troupe", price=150.0, duration_minutes=120,
                            vendor_id=vendor.vendor_id, experience="5y",
                            price_unit="performer")
    db.add_all([flat, per_head, per_performer])
    db.commit()
    db.refresh(flat)
    db.refresh(per_head)
    db.refresh(per_performer)

    out = {
        "user_id": client_user.user_id,
        "vendor_id": vendor.vendor_id,
        "flat_service_id": flat.service_id,
        "per_head_service_id": per_head.service_id,
        "per_performer_service_id": per_performer.service_id,
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
# price_unit is now one of four strings and the column refuses anything else
# (0039). But an alias can still arrive — from iOS, from an old client, from
# anything posting to the API — so the boundary canonicalises it and the two
# functions that read the column are checked against each other regardless.


PER_PERSON_WORDS = [
    "person", "Person", "per person", "Per Person", "persons", "PERSON",
    "head", "per head", "Per Head", "guest", "per guest", "plate", "per plate",
    "pax", "per pax",
]

PER_PERFORMER_WORDS = [
    "performer", "Performer", "per performer", "Per Performer", "performers",
    "PERFORMER", "dancer", "Dancer", "dancers", "per dancer",
    "entertainer", "entertainers", "per entertainer",
]


@pytest.mark.parametrize("word", PER_PERSON_WORDS)
def test_a_per_person_alias_is_stored_as_person(word):
    """Read forgivingly, stored canonical.

    A caterer whose client sends "per head" gets a service priced per person —
    not a service priced flat, which is what happened before and is off by
    however many people are coming.
    """
    from app.routers.services import CreateServiceRequest

    body = CreateServiceRequest(
        name="Catering", price=40.0, experience="5y", price_unit=word
    )
    assert body.price_unit == "person"


@pytest.mark.parametrize("word", ["hour", "hours", "hourly", "per hour", "Per Hourly"])
def test_an_hourly_alias_is_stored_as_hour(word):
    from app.routers.services import CreateServiceRequest

    body = CreateServiceRequest(name="DJ", price=200.0, experience="5y", price_unit=word)
    assert body.price_unit == "hour"


@pytest.mark.parametrize("word", ["day", "days", "per day"])
def test_a_daily_alias_is_stored_as_day(word):
    from app.routers.services import CreateServiceRequest

    body = CreateServiceRequest(name="Tent", price=300.0, experience="5y", price_unit=word)
    assert body.price_unit == "day"


@pytest.mark.parametrize("word", PER_PERFORMER_WORDS)
def test_a_per_performer_alias_is_stored_as_performer(word):
    """A dance troupe whose client sends "per dancer" gets a service priced per
    performer — not flat, which would price a request for 12 dancers the same
    as a request for 2."""
    from app.routers.services import CreateServiceRequest

    body = CreateServiceRequest(
        name="Dance Troupe", price=150.0, experience="5y", price_unit=word
    )
    assert body.price_unit == "performer"


@pytest.mark.parametrize("word", [None, "", "   ", "event", "per event"])
def test_no_unit_and_flat_pricing_both_end_up_meaning_flat(word):
    from app.routers.services import CreateServiceRequest

    body = CreateServiceRequest(name="DJ", price=500.0, experience="5y", price_unit=word)
    assert body.price_unit in (None, "event")


@pytest.mark.parametrize("word", ["widgets", "per widget", "banana", "monthly"])
def test_a_word_we_cannot_charge_against_is_refused(word):
    """It used to be accepted and priced flat, so a vendor could type a word and
    quietly get a total two hundred times smaller than they meant."""
    from pydantic import ValidationError

    from app.routers.services import CreateServiceRequest

    with pytest.raises(ValidationError) as e:
        CreateServiceRequest(name="X", price=1.0, experience="5y", price_unit=word)
    assert "person, hour, day, event" in str(e.value)


def test_the_same_rule_guards_an_edit(plan):
    from pydantic import ValidationError

    from app.routers.services import UpdateServiceRequest

    assert UpdateServiceRequest(price_unit="per head").price_unit == "person"
    with pytest.raises(ValidationError):
        UpdateServiceRequest(price_unit="widgets")


def test_the_column_itself_refuses_anything_else(plan):
    """Belt and braces. The validator is the door; this is the wall — so a code
    path that never goes near the router still can't reintroduce the ambiguity.
    """
    from sqlalchemy.exc import IntegrityError

    db = _db()
    try:
        service = db.query(Service).filter(
            Service.service_id == plan["flat_service_id"]
        ).first()
        service.price_unit = "per head"
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


@pytest.mark.parametrize("unit", ["person", "hour", "day", "event", "performer", None])
def test_the_four_and_nothing_are_all_accepted(plan, unit):
    db = _db()
    try:
        service = db.query(Service).filter(
            Service.service_id == plan["flat_service_id"]
        ).first()
        service.price_unit = unit
        db.commit()
    finally:
        db.close()


def test_a_per_person_service_still_demands_a_headcount(plan):
    """The rule the whole thing exists for, on the value the column now holds."""
    from app.services.booking_service import resolve_total_cents

    db = _db()
    try:
        service = db.query(Service).filter(
            Service.service_id == plan["per_head_service_id"]
        ).first()
        service.price_unit = "person"
        db.commit()

        created = _book(
            plan, db, service_id=plan["per_head_service_id"], guest_count=None
        )
        with pytest.raises(BundleError) as e:
            select_bundle(
                bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
            )
        assert "guest count" in e.value.detail

        booking = db.query(Booking).filter(
            Booking.booking_id == created["booking_id"]
        ).first()
        assert resolve_total_cents(booking, service) is None
    finally:
        db.close()


def test_a_per_performer_service_still_demands_a_performer_count(plan):
    """The same rule, on the newer unit that shares its mechanics with person."""
    from app.services.booking_service import resolve_total_cents

    db = _db()
    try:
        service = db.query(Service).filter(
            Service.service_id == plan["per_performer_service_id"]
        ).first()

        created = _book(
            plan, db, service_id=plan["per_performer_service_id"],
            guest_count=None, performer_count=None,
        )
        with pytest.raises(BundleError) as e:
            select_bundle(
                bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
            )
        assert "performer count" in e.value.detail

        booking = db.query(Booking).filter(
            Booking.booking_id == created["booking_id"]
        ).first()
        assert resolve_total_cents(booking, service) is None
    finally:
        db.close()


def test_an_hourly_service_demands_a_time_window(plan):
    db = _db()
    try:
        service = db.query(Service).filter(
            Service.service_id == plan["flat_service_id"]
        ).first()
        service.price_unit = "hour"
        db.commit()

        created = _book(plan, db, time_start="", time_end="")
        with pytest.raises(BundleError) as e:
            select_bundle(
                bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
            )
        assert "start and end time" in e.value.detail
    finally:
        db.close()


@pytest.mark.parametrize("unit", ["event", None])
def test_a_flat_rate_service_asks_for_no_quantity(plan, unit):
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


def test_a_flat_rate_service_can_opt_into_requiring_a_headcount(plan):
    """require_guest_count is additive: a flat rate normally asks for nothing,
    but a vendor can opt into a headcount anyway."""
    db = _db()
    try:
        service = db.query(Service).filter(
            Service.service_id == plan["flat_service_id"]
        ).first()
        service.price_unit = "event"
        service.require_guest_count = True
        db.commit()

        created = _book(plan, db, guest_count=None)
        with pytest.raises(BundleError) as e:
            select_bundle(
                bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
            )
        assert "guest count" in e.value.detail
    finally:
        db.close()


def test_a_flat_rate_service_can_opt_into_requiring_a_performer_count(plan):
    """Same idea, for require_performer_count."""
    db = _db()
    try:
        service = db.query(Service).filter(
            Service.service_id == plan["flat_service_id"]
        ).first()
        service.price_unit = "event"
        service.require_performer_count = True
        db.commit()

        created = _book(plan, db, guest_count=None, performer_count=None)
        with pytest.raises(BundleError) as e:
            select_bundle(
                bundle_id=created["bundle_id"], caller_user_id=plan["user_id"], db=db
            )
        assert "performer count" in e.value.detail
    finally:
        db.close()


def test_opted_in_and_price_unit_driven_requirements_are_independent(plan):
    """A per-performer service that also opts into requiring a guest count
    demands both — the two checks don't short-circuit each other."""
    db = _db()
    try:
        service = db.query(Service).filter(
            Service.service_id == plan["per_performer_service_id"]
        ).first()
        service.require_guest_count = True
        db.commit()

        created = _book(
            plan, db, service_id=plan["per_performer_service_id"],
            guest_count=None, performer_count=None,
        )
        gaps = booking_gaps(
            db.query(Booking).filter(Booking.booking_id == created["booking_id"]).first(),
            service,
        )
        assert "a guest count" in gaps
        assert "a performer count" in gaps
    finally:
        db.close()


def test_the_gate_and_the_pricer_never_disagree():
    """The property, rather than a list of examples.

    Kept even though the column is constrained now: an alias still arrives at
    the boundary, and the day these two functions disagree about a string is the
    day a booking goes out that nobody can pay for. Examples go stale; this
    doesn't.
    """
    from app.services.booking_service import _normalize_unit
    from app.services.plan_readiness import _price_unit_kind

    words = [
        None, "", "person", "Person", "persons", "per person", "PER PERSON",
        "head", "per head", "guest", "guests", "plate", "plates", "pax",
        "hour", "hours", "hourly", "per hour", "Per Hourly",
        "day", "days", "daily", "per day",
        "performer", "performers", "per performer", "dancer", "dancers",
        "entertainer", "entertainers",
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


# ── TBD is not a time ─────────────────────────────────────────────────


class TestTimesAreRequiredToSend:
    """A vendor accepting is agreeing to be somewhere at a time.

    Times were only ever asked for when the *price* depended on them, which is a
    different question — so a per-event or per-person booking passed the send
    gate carrying "TBD" for both, and vendors were asked to hold a day with no
    hours attached to it. The bundle builder writes exactly that string when it
    wasn't told, and it is non-empty, so every truthiness check read it as an
    answer.
    """

    def _booking(self, **over):
        from app.db.models import Booking

        fields = {
            "date_iso": "2027-06-05",
            "time_start": "18:00",
            "time_end": "23:00",
            "location": "12 Maple Ave, Evanston, IL 60201",
            "guest_count": None,
        }
        fields.update(over)
        return Booking(**fields)

    def _flat_service(self):
        from app.db.models import Service

        return Service(name="DJ", price=1000.0, price_unit="event")

    def test_tbd_times_block_a_flat_rate_booking(self):
        from app.services.plan_readiness import booking_gaps

        gaps = booking_gaps(
            self._booking(time_start="TBD", time_end="TBD"), self._flat_service()
        )
        assert "a start and end time" in gaps

    def test_blank_times_block_it_too(self):
        from app.services.plan_readiness import booking_gaps

        gaps = booking_gaps(
            self._booking(time_start="", time_end=""), self._flat_service()
        )
        assert "a start and end time" in gaps

    def test_one_missing_half_is_still_missing(self):
        from app.services.plan_readiness import booking_gaps

        gaps = booking_gaps(self._booking(time_end="TBD"), self._flat_service())
        assert "a start and end time" in gaps

    def test_real_times_pass(self):
        from app.services.plan_readiness import booking_gaps

        assert booking_gaps(self._booking(), self._flat_service()) == []

    def test_tbd_date_is_still_caught(self):
        """The same placeholder, in the field it was already checked for."""
        from app.services.plan_readiness import booking_gaps

        gaps = booking_gaps(self._booking(date_iso="TBD"), self._flat_service())
        assert "a date" in gaps

    def test_a_plan_with_tbd_times_cannot_be_sent(self):
        """End to end: select_bundle refuses, naming what's missing."""
        import uuid
        from datetime import datetime, timezone
        from app.db.models import Booking, Bundle, Service, User, Vendor
        from app.services.bundle_service import BundleError, select_bundle
        from tests.test_api import TestingSessionLocal

        db = TestingSessionLocal()
        uid = uuid.uuid4().hex[:8]
        client = User(
            email=f"tbd_c_{uid}@test.com", username=f"tbd_c_{uid}", password="pw",
            phone="1", f_name="C", l_name="L", age=30, location="NJ",
            gender="F", language="EN", token_version=0,
        )
        vu = User(
            email=f"tbd_v_{uid}@test.com", username=f"tbd_v_{uid}", password="pw",
            phone="1", f_name="V", l_name="N", age=30, location="NJ",
            gender="F", language="EN", token_version=0,
        )
        db.add_all([client, vu]); db.commit(); db.refresh(client); db.refresh(vu)
        v = Vendor(user_id=vu.user_id, bio="b", category="venue", rating=4.0, num_events=1)
        db.add(v); db.commit(); db.refresh(v)
        svc = Service(name=f"flat-{uid}", price=1000.0, price_unit="event",
                      vendor_id=v.vendor_id, experience="e", category="venue",
                      negotiable=False)
        db.add(svc); db.commit(); db.refresh(svc)
        now = datetime.now(timezone.utc)
        bundle = Bundle(user_id=client.user_id, name="Plan", status="draft",
                        created_at=now, updated_at=now)
        db.add(bundle); db.commit(); db.refresh(bundle)
        bk = Booking(
            user_id=client.user_id, vendor_id=v.vendor_id, service_id=svc.service_id,
            date_iso="2027-06-05", time_start="TBD", time_end="TBD",
            location="12 Maple Ave, Evanston, IL 60201", status="pending",
            bundle_id=bundle.bundle_id,
        )
        db.add(bk); db.commit()

        try:
            with pytest.raises(BundleError) as caught:
                select_bundle(
                    bundle_id=bundle.bundle_id,
                    caller_user_id=client.user_id, db=db,
                )
            assert caught.value.status_code == 400
            assert "start and end time" in caught.value.detail
            db.refresh(bundle)
            assert bundle.status == "draft", "sent anyway"

            # With real hours it goes.
            bk.time_start, bk.time_end = "18:00", "23:00"
            db.commit()
            select_bundle(
                bundle_id=bundle.bundle_id, caller_user_id=client.user_id, db=db
            )
            db.refresh(bundle)
            assert bundle.status == "confirmed"
        finally:
            db.query(Booking).filter(Booking.bundle_id == bundle.bundle_id).delete()
            db.query(Bundle).filter(Bundle.bundle_id == bundle.bundle_id).delete()
            db.query(Service).filter(Service.service_id == svc.service_id).delete()
            db.query(Vendor).filter(Vendor.vendor_id == v.vendor_id).delete()
            db.query(User).filter(
                User.user_id.in_([client.user_id, vu.user_id])
            ).delete(synchronize_session=False)
            db.commit(); db.close()
