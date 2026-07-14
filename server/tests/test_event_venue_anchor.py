"""The event's venue anchor + dependency-orphaning fix.

A bundle's venue booking anchors the whole event's check-in coordinates. When the
venue is removed/refunded, the anchor must clear so the other (traveling) vendors
can no longer check in against a venue the client no longer has.
"""
import uuid
from datetime import datetime, timezone

import pytest

from app.db.models import User, Vendor, Service, Booking, Bundle, Event
from app.services.booking_service import (
    BookingError,
    sync_event_venue,
    _live_venue_booking,
    check_in,
)
from tests.test_api import TestingSessionLocal

VENUE_LAT, VENUE_LNG = 40.7128, -74.0060


def _seed_bundle_with_venue_and_dj():
    """Client bundle+event with a venue booking (coords) and a DJ booking."""
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    def _user(role):
        u = User(
            email=f"anchor_{role}_{uid}@test.com", username=f"anchor_{role}_{uid}",
            password="pw", phone="1", f_name=role, l_name="U",
            age=30, location="NJ", gender="M", language="EN", token_version=0,
        )
        db.add(u); db.commit(); db.refresh(u)
        return u

    client = _user("client")
    venue_vendor_user = _user("venuevendor")
    dj_vendor_user = _user("djvendor")

    venue_vendor = Vendor(user_id=venue_vendor_user.user_id, bio="v", rating=4.5, num_events=1,
                          category="venue")
    dj_vendor = Vendor(user_id=dj_vendor_user.user_id, bio="d", rating=4.5, num_events=1,
                       category="music_entertainment", subcategory="dj")
    db.add_all([venue_vendor, dj_vendor]); db.commit()
    db.refresh(venue_vendor); db.refresh(dj_vendor)

    venue_service = Service(
        name="Grand Hall", price=5000.0, duration_minutes=600, vendor_id=venue_vendor.vendor_id,
        experience="e", category="venue", location="1 Main St",
        venue_latitude=VENUE_LAT, venue_longitude=VENUE_LNG,
    )
    dj_service = Service(
        name="DJ Set", price=1000.0, duration_minutes=240, vendor_id=dj_vendor.vendor_id,
        experience="e", category="music_entertainment", subcategory="dj",
    )
    db.add_all([venue_service, dj_service]); db.commit()
    db.refresh(venue_service); db.refresh(dj_service)

    event = Event(user_id=client.user_id, name="Wedding", date_iso="2026-10-01", location="TBD")
    db.add(event); db.commit(); db.refresh(event)

    _now = datetime.now(timezone.utc)
    bundle = Bundle(user_id=client.user_id, name="Wedding Bundle", event_id=event.event_id,
                    status="draft", created_at=_now, updated_at=_now)
    db.add(bundle); db.commit(); db.refresh(bundle)

    def _booking(vendor, service):
        b = Booking(
            user_id=client.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
            time_start="18:00", time_end="23:00", location="TBD", date_iso="2026-10-01",
            status="approved", payment_status="paid", bundle_id=bundle.bundle_id,
        )
        db.add(b); db.commit(); db.refresh(b)
        return b

    venue_booking = _booking(venue_vendor, venue_service)
    dj_booking = _booking(dj_vendor, dj_service)

    return {
        "db": db, "client": client, "event": event, "bundle": bundle,
        "venue_booking": venue_booking, "dj_booking": dj_booking,
    }


def test_venue_anchors_event_and_mirrors_to_dj():
    s = _seed_bundle_with_venue_and_dj()
    db, event, dj = s["db"], s["event"], s["dj_booking"]

    sync_event_venue(s["bundle"].bundle_id, db)
    db.commit()
    db.refresh(event); db.refresh(dj)

    # Event holds the venue coords; the DJ's booking mirrors them for check-in.
    assert event.venue_latitude == VENUE_LAT
    assert event.venue_longitude == VENUE_LNG
    assert dj.venue_latitude == VENUE_LAT
    assert dj.venue_longitude == VENUE_LNG

    # The DJ can check in at the venue while it's booked.
    result = check_in(
        booking_id=dj.booking_id, caller_user_id=s["client"].user_id,
        latitude=VENUE_LAT, longitude=VENUE_LNG, db=db,
    )
    assert result["distance_miles"] == 0.0
    db.close()


def test_removing_venue_clears_anchor_and_blocks_dj_checkin():
    s = _seed_bundle_with_venue_and_dj()
    db, event, dj = s["db"], s["event"], s["dj_booking"]

    sync_event_venue(s["bundle"].bundle_id, db)
    db.commit()

    # The venue falls through — simulate removal by deleting its booking.
    db.delete(s["venue_booking"])
    db.commit()
    assert _live_venue_booking(s["bundle"].bundle_id, db) is None

    sync_event_venue(s["bundle"].bundle_id, db)
    db.commit()
    db.refresh(event); db.refresh(dj)

    # The anchor is gone everywhere...
    assert event.venue_latitude is None
    assert dj.venue_latitude is None

    # ...so the DJ can no longer check in (and therefore can't trigger a payout)
    # against a venue the client no longer has.
    with pytest.raises(BookingError) as exc:
        check_in(
            booking_id=dj.booking_id, caller_user_id=s["client"].user_id,
            latitude=VENUE_LAT, longitude=VENUE_LNG, db=db,
        )
    assert exc.value.status_code == 400
    db.close()


def test_refunded_venue_stops_anchoring():
    s = _seed_bundle_with_venue_and_dj()
    db = s["db"]

    sync_event_venue(s["bundle"].bundle_id, db)
    db.commit()
    assert _live_venue_booking(s["bundle"].bundle_id, db) is not None

    # A refunded venue no longer anchors, even though the booking row still exists.
    s["venue_booking"].payment_status = "refunded"
    db.commit()
    assert _live_venue_booking(s["bundle"].bundle_id, db) is None

    sync_event_venue(s["bundle"].bundle_id, db)
    db.commit()
    db.refresh(s["event"])
    assert s["event"].venue_latitude is None
    db.close()
