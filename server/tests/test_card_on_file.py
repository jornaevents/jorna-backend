"""The card is saved when a plan is sent and charged when a vendor accepts.

Payment used to be the client's job after the fact — send, wait, then come back
and press Pay on each vendor who accepted. This moves where the card is
captured, not when money moves: the charge still happens on acceptance.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.db.models import Booking, Bundle, Service, User, Vendor
from app.models.schemas import BookingStatus
from app.services.booking_service import update_booking_status
from app.services.stripe_service import CardChargeUnavailable, charge_saved_card
from tests.test_api import TestingSessionLocal


@pytest.fixture
def booked():
    """A pending booking whose vendor can receive payouts, client card on file."""
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]
    client_user = User(
        email=f"cc_{uid}@t.com", username=f"cc_{uid}", password="pw", phone="1",
        f_name="C", l_name="L", age=30, location="x", gender="M",
        language="EN", token_version=0,
        stripe_customer_id=f"cus_{uid}", stripe_payment_method_id=f"pm_{uid}",
        card_brand="visa", card_last4="4242",
    )
    vendor_user = User(
        email=f"cv_{uid}@t.com", username=f"cv_{uid}", password="pw", phone="1",
        f_name="V", l_name="N", age=30, location="x", gender="F",
        language="EN", token_version=0,
    )
    db.add_all([client_user, vendor_user])
    db.commit()
    db.refresh(client_user)
    db.refresh(vendor_user)

    vendor = Vendor(user_id=vendor_user.user_id, bio="b", rating=5.0, num_events=1,
                    stripe_account_id=f"acct_{uid}", stripe_onboarding_complete=True)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    service = Service(name="DJ", price=800.0, duration_minutes=180,
                      vendor_id=vendor.vendor_id, experience="5y", price_unit="event")
    db.add(service)
    db.commit()
    db.refresh(service)

    now = datetime.now(timezone.utc)
    bundle = Bundle(user_id=client_user.user_id, name="Plan", status="confirmed",
                    created_at=now, updated_at=now)
    db.add(bundle)
    db.commit()
    db.refresh(bundle)

    booking = Booking(
        user_id=client_user.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id, time_start="18:00", time_end="23:00",
        location="Hall", date_iso="2027-08-14", status="pending",
        bundle_id=bundle.bundle_id, currency="usd",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    out = {
        "booking_id": booking.booking_id,
        "client_id": client_user.user_id,
        "vendor_user_id": vendor_user.user_id,
    }
    db.close()
    yield out


def _status(booking_id):
    db = TestingSessionLocal()
    try:
        b = db.query(Booking).filter(Booking.booking_id == booking_id).first()
        return b.status, b.payment_status, b.amount_cents
    finally:
        db.close()


class _Succeeded:
    id = "pi_ok"
    status = "succeeded"


def _accept(booked, db=None):
    own = db is None
    db = db or TestingSessionLocal()
    try:
        return update_booking_status(
            booking_id=booked["booking_id"],
            caller_user_id=booked["vendor_user_id"],
            status=BookingStatus.APPROVED,
            db=db,
        )
    finally:
        if own:
            db.close()


def test_accepting_charges_the_saved_card(mocker, booked):
    create = mocker.patch("stripe.PaymentIntent.create", return_value=_Succeeded())
    mocker.patch("app.services.booking_service._dispatch_status_notification")

    _accept(booked)

    assert create.call_count == 1
    kwargs = create.call_args.kwargs
    assert kwargs["off_session"] is True
    assert kwargs["confirm"] is True
    assert kwargs["amount"] == 80000
    assert kwargs["transfer_group"] == booked["booking_id"]

    assert _status(booked["booking_id"]) == ("approved", "paid", 80000)


def test_a_declined_card_leaves_the_booking_approved_and_payable(mocker, booked):
    """A vendor's answer is not the place to surface a card problem. The booking
    lands where the manual Pay button already handles it."""
    import stripe

    mocker.patch(
        "stripe.PaymentIntent.create",
        side_effect=stripe.CardError("Your card was declined.", None, "card_declined"),
    )
    mocker.patch("app.services.booking_service._dispatch_status_notification")

    _accept(booked)

    status, payment_status, _ = _status(booked["booking_id"])
    assert (status, payment_status) == ("approved", "unpaid")


def test_no_card_on_file_is_not_an_error(mocker, booked):
    """Nothing to charge, so nothing is tried — and the acceptance still stands."""
    create = mocker.patch("stripe.PaymentIntent.create", return_value=_Succeeded())
    mocker.patch("app.services.booking_service._dispatch_status_notification")

    db = TestingSessionLocal()
    user = db.query(User).filter(User.user_id == booked["client_id"]).first()
    user.stripe_payment_method_id = None
    db.commit()
    db.close()

    _accept(booked)

    assert create.call_count == 0
    assert _status(booked["booking_id"])[:2] == ("approved", "unpaid")


def test_declining_charges_nothing(mocker, booked):
    create = mocker.patch("stripe.PaymentIntent.create", return_value=_Succeeded())
    mocker.patch("app.services.booking_service._dispatch_status_notification")

    db = TestingSessionLocal()
    try:
        update_booking_status(
            booking_id=booked["booking_id"],
            caller_user_id=booked["vendor_user_id"],
            status=BookingStatus.REJECTED,
            db=db,
        )
    finally:
        db.close()

    assert create.call_count == 0


def test_an_already_paid_booking_is_never_charged_twice(mocker, booked):
    mocker.patch("stripe.PaymentIntent.create", return_value=_Succeeded())

    db = TestingSessionLocal()
    b = db.query(Booking).filter(Booking.booking_id == booked["booking_id"]).first()
    b.payment_status = "paid"
    db.commit()
    db.close()

    db = TestingSessionLocal()
    try:
        with pytest.raises(CardChargeUnavailable):
            charge_saved_card(booking_id=booked["booking_id"], db=db)
    finally:
        db.close()


def test_a_total_that_cannot_be_worked_out_is_not_charged(mocker, booked):
    """Per person with no headcount: refuse rather than charge the rate as a
    total. The same guard the manual path has always had."""
    mocker.patch("stripe.PaymentIntent.create", return_value=_Succeeded())

    db = TestingSessionLocal()
    b = db.query(Booking).filter(Booking.booking_id == booked["booking_id"]).first()
    svc = db.query(Service).filter(Service.service_id == b.service_id).first()
    svc.price_unit = "person"
    b.guest_count = None
    b.amount_cents = None
    db.commit()
    db.close()

    db = TestingSessionLocal()
    try:
        with pytest.raises(CardChargeUnavailable):
            charge_saved_card(booking_id=booked["booking_id"], db=db)
    finally:
        db.close()


def test_the_charge_is_idempotent_per_booking(mocker, booked):
    """Acceptance can be retried; the client is charged once."""
    create = mocker.patch("stripe.PaymentIntent.create", return_value=_Succeeded())

    db = TestingSessionLocal()
    try:
        charge_saved_card(booking_id=booked["booking_id"], db=db)
    finally:
        db.close()

    expected = "accept_" + booked["booking_id"]
    assert create.call_args.kwargs["idempotency_key"] == expected
