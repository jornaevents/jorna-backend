"""A vendor's day is not a single booking.

The conflict check used to work in whole calendar days: one confirmed booking
on a date and every other request for that date was refused. A photographer who
shoots a morning ceremony was thereby unavailable for an evening reception,
which is not how any of these trades work.
"""

import uuid

from app.db.models import Booking, Service, User, Vendor
from app.services.booking_service import vendor_has_conflicting_booking
from tests.test_api import TestingSessionLocal

DAY = "2026-09-05"
NEXT = "2026-09-06"


def _vendor_with(existing):
    """A vendor holding one confirmed booking, described by `existing`."""
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]
    user = User(
        email=f"sd_{uid}@test.com", username=f"sd_{uid}", password="pw",
        phone="1", f_name="A", l_name="B", age=30, location="x",
        gender="M", language="EN", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    vendor = Vendor(user_id=user.user_id, bio="b", rating=5.0, num_events=1)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    service = Service(name="Photos", price=800.0, duration_minutes=240,
                      vendor_id=vendor.vendor_id, experience="5y")
    db.add(service)
    db.commit()
    db.refresh(service)

    fields = {"date_iso": DAY, "date_end": None, "time_start": "09:00",
              "time_end": "13:00", "status": "approved"}
    fields.update(existing)
    db.add(Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id, location="Hall", **fields,
    ))
    db.commit()
    vendor_id = vendor.vendor_id
    db.close()
    return vendor_id


def _clashes(vendor_id, **requested):
    fields = {"date_iso": DAY, "date_end": None, "time_start": "18:00", "time_end": "23:00"}
    fields.update(requested)
    db = TestingSessionLocal()
    try:
        return vendor_has_conflicting_booking(vendor_id=vendor_id, db=db, **fields) is not None
    finally:
        db.close()


def test_a_second_job_the_same_day_is_allowed():
    """Morning ceremony, evening reception."""
    vendor_id = _vendor_with({"time_start": "09:00", "time_end": "13:00"})
    assert _clashes(vendor_id, time_start="18:00", time_end="23:00") is False


def test_overlapping_hours_still_clash():
    vendor_id = _vendor_with({"time_start": "09:00", "time_end": "13:00"})
    assert _clashes(vendor_id, time_start="12:00", time_end="16:00") is True


def test_one_inside_the_other_clashes():
    vendor_id = _vendor_with({"time_start": "09:00", "time_end": "18:00"})
    assert _clashes(vendor_id, time_start="12:00", time_end="14:00") is True


def test_touching_hours_are_free():
    """Ends at 14:00, the next starts at 14:00. Whether they can cross town in
    no time is the vendor's call — they're the one accepting."""
    vendor_id = _vendor_with({"time_start": "09:00", "time_end": "14:00"})
    assert _clashes(vendor_id, time_start="14:00", time_end="18:00") is False


def test_an_unknown_time_stays_a_clash():
    """Nothing can be shown not to overlap, and the alternative is double-booking."""
    vendor_id = _vendor_with({"time_start": "TBD", "time_end": "TBD"})
    assert _clashes(vendor_id, time_start="18:00", time_end="23:00") is True

    known = _vendor_with({"time_start": "09:00", "time_end": "13:00"})
    assert _clashes(known, time_start="TBD", time_end="TBD") is True


def test_a_window_crossing_midnight_is_not_a_negative_span():
    """22:00–02:00 runs four hours into the next day, not backwards through
    twenty. An afternoon job doesn't touch it; a late one does."""
    vendor_id = _vendor_with({"time_start": "22:00", "time_end": "02:00"})
    assert _clashes(vendor_id, time_start="13:00", time_end="17:00") is False
    assert _clashes(vendor_id, time_start="21:00", time_end="23:00") is True


def test_a_multi_day_booking_still_takes_whole_days():
    """A Friday-to-Sunday hire occupies the Saturday completely; there are no
    hours on it that would say otherwise."""
    vendor_id = _vendor_with({"date_iso": "2026-09-04", "date_end": "2026-09-06"})
    assert _clashes(vendor_id, date_iso=DAY, time_start="18:00", time_end="23:00") is True


def test_a_multi_day_request_takes_whole_days_too():
    vendor_id = _vendor_with({"time_start": "09:00", "time_end": "13:00"})
    assert _clashes(vendor_id, date_iso=DAY, date_end=NEXT,
                    time_start="18:00", time_end="23:00") is True


def test_a_different_day_never_clashes():
    vendor_id = _vendor_with({"time_start": "09:00", "time_end": "23:00"})
    assert _clashes(vendor_id, date_iso=NEXT, time_start="09:00", time_end="23:00") is False


def test_only_confirmed_bookings_lock_the_hours():
    """A pending request is a lead, not a commitment — unchanged."""
    vendor_id = _vendor_with({"status": "pending", "time_start": "18:00", "time_end": "23:00"})
    assert _clashes(vendor_id, time_start="18:00", time_end="23:00") is False


def test_a_tbd_date_cannot_clash():
    vendor_id = _vendor_with({"time_start": "09:00", "time_end": "23:00"})
    assert _clashes(vendor_id, date_iso="TBD") is False
