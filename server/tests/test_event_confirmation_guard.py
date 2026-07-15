"""Escrow can't be confirmed/released before the event has taken place (#4).

Funds are held until the event date arrives. Neither party can confirm (and so
trigger release) before then, and a TBD-dated booking isn't confirmable until a
real date is set.
"""
import uuid

import pytest

from app.db.models import User, Vendor, Service, Booking
from app.services.booking_service import event_confirmable_date
from app.services.stripe_service import confirm_event, StripeError
from tests.test_api import TestingSessionLocal


def _seed_paid_booking(date_iso="2099-01-01", date_end=None):
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]
    client = User(email=f"cg_client_{uid}@t.com", username=f"cg_client_{uid}", password="pw",
                  phone="1", f_name="C", l_name="U", age=30, location="NJ", gender="M",
                  language="EN", token_version=0)
    vuser = User(email=f"cg_vendor_{uid}@t.com", username=f"cg_vendor_{uid}", password="pw",
                 phone="1", f_name="V", l_name="U", age=30, location="NJ", gender="F",
                 language="EN", token_version=0)
    db.add_all([client, vuser]); db.commit(); db.refresh(client); db.refresh(vuser)
    vendor = Vendor(user_id=vuser.user_id, bio="b", rating=4.5, num_events=1)
    db.add(vendor); db.commit(); db.refresh(vendor)
    service = Service(name="DJ", price=1000.0, duration_minutes=120,
                      vendor_id=vendor.vendor_id, experience="e")
    db.add(service); db.commit(); db.refresh(service)
    booking = Booking(
        user_id=client.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="18:00", time_end="23:00", location="Hall",
        date_iso=date_iso, date_end=date_end, status="payment_confirmed",
        payment_status="paid", amount_cents=100000, platform_fee_cents=5000,
    )
    db.add(booking); db.commit(); db.refresh(booking)
    return db, client, booking


def test_cannot_confirm_before_event_date():
    db, client, booking = _seed_paid_booking(date_iso="2099-01-01")
    with pytest.raises(StripeError) as exc:
        confirm_event(booking_id=booking.booking_id, caller_user_id=client.user_id, db=db)
    assert exc.value.status_code == 400
    db.refresh(booking)
    assert booking.customer_confirmed_at is None
    db.close()


def test_can_confirm_on_or_after_event_date():
    db, client, booking = _seed_paid_booking(date_iso="2000-01-01")
    confirm_event(booking_id=booking.booking_id, caller_user_id=client.user_id, db=db)
    db.refresh(booking)
    assert booking.customer_confirmed_at is not None
    # Only one party confirmed → funds stay held, not released.
    assert booking.payment_status == "paid"
    db.close()


def test_tbd_date_blocks_confirmation():
    db, client, booking = _seed_paid_booking(date_iso="TBD", date_end=None)
    ok, msg = event_confirmable_date(booking)
    assert ok is False and "date" in msg.lower()
    with pytest.raises(StripeError) as exc:
        confirm_event(booking_id=booking.booking_id, caller_user_id=client.user_id, db=db)
    assert exc.value.status_code == 400
    db.close()


def test_multiday_gates_on_end_date():
    # Started in the past but ends in the future → the event isn't over yet.
    db, _client, booking = _seed_paid_booking(date_iso="2000-01-01", date_end="2099-01-01")
    ok, _ = event_confirmable_date(booking)
    assert ok is False
    db.close()
