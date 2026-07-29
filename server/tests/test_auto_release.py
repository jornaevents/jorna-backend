"""Escrow that releases itself when the client goes quiet.

Both parties must confirm before money moves, so a client who simply stops
opening the app holds a vendor's payment indefinitely. After a week past the
event, silence counts as assent — unless the client has said something.
"""

import uuid
from datetime import datetime, timedelta, timezone

from app.db.models import Booking, Service, User, Vendor
from app.services.stripe_service import AUTO_RELEASE_DAYS, auto_release_due
from tests.test_api import TestingSessionLocal


def _day(offset: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset)).date().isoformat()


def _booking(**over):
    """A paid booking the vendor has confirmed, a fortnight after the event."""
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]
    client_user = User(
        email=f"ar_c_{uid}@test.com", username=f"ar_c_{uid}", password="pw",
        phone="1", f_name="C", l_name="L", age=30, location="x",
        gender="M", language="EN", token_version=0,
    )
    vendor_user = User(
        email=f"ar_v_{uid}@test.com", username=f"ar_v_{uid}", password="pw",
        phone="1", f_name="V", l_name="N", age=30, location="x",
        gender="F", language="EN", token_version=0,
    )
    db.add_all([client_user, vendor_user])
    db.commit()
    db.refresh(client_user)
    db.refresh(vendor_user)

    vendor = Vendor(
        user_id=vendor_user.user_id, bio="b", rating=5.0, num_events=1,
        stripe_account_id=over.pop("stripe_account_id", f"acct_{uid}"),
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    service = Service(name="DJ", price=500.0, duration_minutes=180,
                      vendor_id=vendor.vendor_id, experience="5y")
    db.add(service)
    db.commit()
    db.refresh(service)

    fields = {
        "date_iso": _day(-14),
        "status": "payment_confirmed",
        "payment_status": "paid",
        "vendor_confirmed_at": datetime.now(timezone.utc),
        "customer_confirmed_at": None,
        "amount_cents": 50000,
        "platform_fee_cents": 5000,
        "currency": "usd",
    }
    fields.update(over)

    booking = Booking(
        user_id=client_user.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id, time_start="18:00", time_end="23:00",
        location="Hall", **fields,
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    booking_id = booking.booking_id
    db.close()
    return booking_id


def _sweep():
    db = TestingSessionLocal()
    try:
        return auto_release_due(db=db)
    finally:
        db.close()


def _status(booking_id):
    db = TestingSessionLocal()
    try:
        b = db.query(Booking).filter(Booking.booking_id == booking_id).first()
        return b.payment_status, b.customer_confirmed_at, b.funds_released_at
    finally:
        db.close()


def test_a_week_of_silence_releases_the_money(mocker):
    transfer = mocker.patch("stripe.Transfer.create")
    booking_id = _booking()

    result = _sweep()
    assert booking_id in result["released"]
    assert transfer.call_count == 1
    # Net of the platform fee, as a confirmed release would be.
    assert transfer.call_args.kwargs["amount"] == 45000

    status, customer_confirmed, released_at = _status(booking_id)
    assert status == "released"
    assert released_at is not None
    # The client never confirmed, so nothing says they did — that absence is
    # what tells a later reader this was automatic.
    assert customer_confirmed is None


def test_not_before_the_week_is_up(mocker):
    mocker.patch("stripe.Transfer.create")
    booking_id = _booking(date_iso=_day(-(AUTO_RELEASE_DAYS - 1)))
    assert booking_id not in _sweep()["released"]
    assert _status(booking_id)[0] == "paid"


def test_a_multi_day_booking_counts_from_its_last_day(mocker):
    """Started three weeks ago, finished two days ago: not due."""
    mocker.patch("stripe.Transfer.create")
    booking_id = _booking(date_iso=_day(-21), date_end=_day(-2))
    assert booking_id not in _sweep()["released"]


def test_a_dispute_stops_it(mocker):
    """raise_dispute has always promised this. Now something honours it."""
    mocker.patch("stripe.Transfer.create")
    booking_id = _booking(payment_status="disputed")
    assert booking_id not in _sweep()["released"]
    assert _status(booking_id)[0] == "disputed"


def test_a_refund_stops_it(mocker):
    mocker.patch("stripe.Transfer.create")
    booking_id = _booking(payment_status="refunded")
    assert booking_id not in _sweep()["released"]


def test_a_vendor_who_never_confirmed_is_not_paid(mocker):
    """The vendor's confirmation is their word that they turned up. Paying
    without it would settle a no-show against a client who wasn't looking."""
    mocker.patch("stripe.Transfer.create")
    booking_id = _booking(vendor_confirmed_at=None)
    assert booking_id not in _sweep()["released"]
    assert _status(booking_id)[0] == "paid"


def test_an_event_with_no_real_date_never_qualifies(mocker):
    mocker.patch("stripe.Transfer.create")
    booking_id = _booking(date_iso="TBD")
    assert booking_id not in _sweep()["released"]


def test_an_already_released_booking_is_left_alone(mocker):
    transfer = mocker.patch("stripe.Transfer.create")
    booking_id = _booking(payment_status="released")
    assert booking_id not in _sweep()["released"]
    assert transfer.call_count == 0


def test_one_broken_payout_doesnt_strand_the_others(mocker):
    """A vendor with no Stripe account fails alone."""
    mocker.patch("stripe.Transfer.create")
    broken = _booking(stripe_account_id=None)
    fine = _booking()

    result = _sweep()
    assert broken in result["failed"]
    assert fine in result["released"]
    assert _status(fine)[0] == "released"
    assert _status(broken)[0] == "paid"


def test_the_sweep_is_safe_to_run_twice(mocker):
    transfer = mocker.patch("stripe.Transfer.create")
    booking_id = _booking()

    assert booking_id in _sweep()["released"]
    assert booking_id not in _sweep()["released"]
    assert transfer.call_count == 1


# ── Confirming before the booking's scheduled end ──────────────────────
#
# A booking runs to the time it was written down for; a job frequently doesn't.
# Once the day has arrived and the vendor has checked in, the client can settle
# up rather than waiting on a date.

from app.services.booking_service import event_confirmable_date
from app.db.models import Booking as BookingModel


def _b(**over) -> BookingModel:
    fields = {"date_iso": _day(0), "date_end": None, "vendor_checked_in_at": None}
    fields.update(over)
    return BookingModel(**fields)


def test_after_the_last_day_needs_no_check_in():
    ok, msg = event_confirmable_date(_b(date_iso=_day(-1)))
    assert ok and msg is None


def test_mid_booking_with_a_check_in_is_confirmable():
    """Day one of three, vendor has arrived: the client needn't wait two more."""
    ok, _ = event_confirmable_date(
        _b(date_iso=_day(-1), date_end=_day(1), vendor_checked_in_at="2026-07-28T18:00:00Z")
    )
    assert ok


def test_mid_booking_without_a_check_in_is_not():
    ok, msg = event_confirmable_date(_b(date_iso=_day(-1), date_end=_day(1)))
    assert not ok
    assert "checked in at the venue" in msg


def test_a_check_in_before_the_day_doesnt_open_it():
    """A vendor can check in early — dropping equipment the night before. That
    isn't the event happening."""
    ok, msg = event_confirmable_date(
        _b(date_iso=_day(2), date_end=_day(4), vendor_checked_in_at="2026-07-28T18:00:00Z")
    )
    assert not ok
    assert "on or after" in msg


def test_a_single_day_booking_opens_on_the_day_once_they_arrive():
    ok, _ = event_confirmable_date(
        _b(date_iso=_day(0), vendor_checked_in_at="2026-07-28T18:00:00Z")
    )
    assert ok


def test_no_date_is_never_confirmable_however_it_went():
    ok, msg = event_confirmable_date(
        _b(date_iso="TBD", vendor_checked_in_at="2026-07-28T18:00:00Z")
    )
    assert not ok
    assert "no scheduled date" in msg
