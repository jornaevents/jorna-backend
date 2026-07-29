"""The three builder options are alternatives, not competitors.

Each option is saved as it's built, and saving writes real bookings. The
availability check read those back, so option two found option one's vendors
taken and option three found both — three plans competing for the same vendors,
with the first one always winning.
"""

import uuid

from app.db.models import Booking, Service, User, Vendor
from app.models.chatbot_schemas import BundleRequest
from app.services.chatbot_service import generate_multi_bundle
from tests.test_api import TestingSessionLocal

DAY = "2027-05-01"

# Slots nothing else in the suite seeds, so "one vendor" here means one. The
# shared test database is seeded by every other file too, and photography or
# catering would quietly have a dozen candidates.
SLOTS = ["videography", "cultural_services"]


def _clear_slots(db):
    """Empty the two slots this file uses.

    Every test here turns on scarcity — "one videographer" has to mean one — and
    the test database is shared, including with the earlier tests in this file.
    Safe because nothing else in the suite seeds these categories, which is why
    they were chosen.
    """
    from app.models.chatbot_schemas import CHATBOT_SLOTS

    categories = [CHATBOT_SLOTS[s]["category"] for s in SLOTS]
    services = db.query(Service).filter(Service.category.in_(categories)).all()
    for service in services:
        db.query(Booking).filter(Booking.service_id == service.service_id).delete()
        db.delete(service)
    db.commit()


def _world(vendors_per_category=1):
    """A client, and `vendors_per_category` vendors in each of SLOTS."""
    db = TestingSessionLocal()
    _clear_slots(db)
    uid = str(uuid.uuid4())[:6]
    client_user = User(
        email=f"mb_{uid}@t.com", username=f"mb_{uid}", password="pw", phone="1",
        f_name="C", l_name="L", age=30, location="x", gender="M",
        language="EN", token_version=0,
    )
    db.add(client_user)
    db.commit()
    db.refresh(client_user)

    made = []
    for i in range(vendors_per_category):
        for cat in SLOTS:
            u = User(
                email=f"mv{i}{cat}_{uid}@t.com", username=f"mv{i}{cat}_{uid}", password="pw",
                phone="1", f_name=cat[:5], l_name=str(i), age=30, location="x",
                gender="F", language="EN", token_version=0,
            )
            db.add(u)
            db.commit()
            db.refresh(u)
            v = Vendor(user_id=u.user_id, bio="b", rating=4.0 + i * 0.3, num_events=5)
            db.add(v)
            db.commit()
            db.refresh(v)
            db.add(Service(
                name=f"{cat} {i}", price=500.0 + i * 400, duration_minutes=180,
                vendor_id=v.vendor_id, experience="5y", category=cat, negotiable=False,
            ))
            db.commit()
            made.append((v.vendor_id, cat))
    user_id = client_user.user_id
    db.close()
    return user_id, made


def _options(user_id):
    db = TestingSessionLocal()
    try:
        req = BundleRequest(
            needed_categories=SLOTS, booked_categories=[], event_date=DAY,
        )
        res = generate_multi_bundle(req, db=db, user_id=user_id)
        return [[i.vendor_name for i in o.bundle.items] for o in res.options]
    finally:
        db.close()


def test_one_vendor_per_slot_still_fills_all_three_options():
    """The sole photographer belongs in every option. Before, the first bundle
    took them and the other two came back empty."""
    user_id, _ = _world(vendors_per_category=1)
    filled = _options(user_id)
    assert [len(names) for names in filled] == [2, 2, 2], filled


def test_two_vendors_per_slot_fills_all_three():
    """The third option used to be the one left with nothing."""
    user_id, _ = _world(vendors_per_category=2)
    assert [len(n) for n in _options(user_id)] == [2, 2, 2]


def test_options_may_share_a_vendor():
    """If one vendor is the best answer at more than one price point, the tiers
    should differ where there's real choice — not by being denied them."""
    user_id, _ = _world(vendors_per_category=1)
    filled = _options(user_id)
    assert filled[0] == filled[1] == filled[2]


def _book(vendor_id, user_id, status, time_start="09:00", time_end="13:00"):
    db = TestingSessionLocal()
    service = db.query(Service).filter(Service.vendor_id == vendor_id).first()
    db.add(Booking(
        user_id=user_id, vendor_id=vendor_id, service_id=service.service_id,
        date_iso=DAY, time_start=time_start, time_end=time_end,
        location="Hall", status=status,
    ))
    db.commit()
    db.close()


def test_a_vendor_locked_for_the_day_is_still_kept_out():
    """The check that matters is unchanged."""
    user_id, made = _world(vendors_per_category=1)
    photographer = next(v for v, cat in made if cat == SLOTS[0])
    _book(photographer, user_id, "approved")

    # The other slot still fills; this one has nobody left.
    for names in _options(user_id):
        assert len(names) == 1, names


def test_a_pending_request_does_not_keep_a_vendor_out():
    """A pending request is a lead — nothing stops a vendor answering two. It
    used to hide them from every other client's builder, and since this builder
    writes pending bookings for every option it generates, an abandoned draft
    went on hiding its vendors indefinitely."""
    user_id, made = _world(vendors_per_category=1)
    photographer = next(v for v, cat in made if cat == SLOTS[0])
    _book(photographer, user_id, "pending")

    for names in _options(user_id):
        assert len(names) == 2, names


# ── Optional hours ─────────────────────────────────────────────────────


def _options_at(user_id, time_start=None, time_end=None):
    db = TestingSessionLocal()
    try:
        req = BundleRequest(
            needed_categories=SLOTS, booked_categories=[], event_date=DAY,
            time_start=time_start, time_end=time_end,
        )
        res = generate_multi_bundle(req, db=db, user_id=user_id)
        return [[i.vendor_name for i in o.bundle.items] for o in res.options]
    finally:
        db.close()


def test_the_hours_reach_the_bookings():
    """Given times, the builder writes them instead of TBD — which is what lets
    a vendor with a morning job be offered an evening one."""
    user_id, _ = _world(vendors_per_category=1)
    _options_at(user_id, "18:00", "23:00")

    db = TestingSessionLocal()
    made = db.query(Booking).filter(Booking.user_id == user_id).all()
    assert made, "the builder should have written bookings"
    assert all(b.time_start == "18:00" and b.time_end == "23:00" for b in made)
    db.close()


def test_without_times_the_bookings_are_tbd():
    """Optional means optional: the bundle still builds."""
    user_id, _ = _world(vendors_per_category=1)
    filled = _options_at(user_id)
    assert [len(n) for n in filled] == [2, 2, 2]

    db = TestingSessionLocal()
    made = db.query(Booking).filter(Booking.user_id == user_id).all()
    assert all(b.time_start == "TBD" for b in made)
    db.close()


def test_a_vendor_busy_in_the_morning_is_offered_for_the_evening():
    """The point of asking. Whole-day availability turned this vendor down."""
    user_id, made = _world(vendors_per_category=1)
    videographer = next(v for v, cat in made if cat == SLOTS[0])
    _book(videographer, user_id, "approved", time_start="09:00", time_end="13:00")

    for names in _options_at(user_id, "18:00", "23:00"):
        assert len(names) == 2, names


def test_and_still_turned_down_for_a_clashing_evening():
    user_id, made = _world(vendors_per_category=1)
    videographer = next(v for v, cat in made if cat == SLOTS[0])
    _book(videographer, user_id, "approved", time_start="17:00", time_end="21:00")

    for names in _options_at(user_id, "18:00", "23:00"):
        assert len(names) == 1, names
