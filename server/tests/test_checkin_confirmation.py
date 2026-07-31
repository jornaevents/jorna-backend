"""A vendor's GPS check-in confirms their side even before the event date.

Regression for the stranding bug: with the event-date release guard (#4), an
early or day-1-of-multi-day check-in recorded presence but never set
vendor_confirmed_at, so a later customer confirmation couldn't release. The
vendor's check-in now confirms whenever they're at the venue; premature release
is still prevented by the customer's date-gated confirmation.
"""
import uuid
from datetime import datetime, timezone

from app.db.models import User, Vendor, Service, Booking, Bundle, Event
from app.services.booking_service import check_in
from app.services.stripe_service import confirm_event

from tests.test_api import TestingSessionLocal

VLAT, VLNG = 40.0, -74.0


def _seed_paid_venue_booking(date_iso: str, stripe_ready: bool = False):
    db = TestingSessionLocal()
    uid = uuid.uuid4().hex[:8]
    client = User(email=f"ci_c_{uid}@t.com", username=f"ci_c_{uid}", password="pw", phone="1",
                  f_name="C", l_name="U", age=30, location="NJ", gender="M", language="EN", token_version=0)
    vuser = User(email=f"ci_v_{uid}@t.com", username=f"ci_v_{uid}", password="pw", phone="1",
                 f_name="V", l_name="U", age=30, location="NJ", gender="F", language="EN", token_version=0)
    db.add_all([client, vuser]); db.commit(); db.refresh(client); db.refresh(vuser)

    vendor = Vendor(user_id=vuser.user_id, bio="b", rating=4.5, num_events=1, category="venue",
                    stripe_account_id=("acct_test" if stripe_ready else None),
                    stripe_onboarding_complete=stripe_ready)
    db.add(vendor); db.commit(); db.refresh(vendor)

    svc = Service(name="Hall", price=5000.0, duration_minutes=600, vendor_id=vendor.vendor_id,
                  experience="e", category="venue", location="1 Main",
                  venue_latitude=VLAT, venue_longitude=VLNG)
    db.add(svc); db.commit(); db.refresh(svc)

    now = datetime.now(timezone.utc)
    event = Event(user_id=client.user_id, name="Wedding", date_iso=date_iso, location="1 Main")
    db.add(event); db.commit(); db.refresh(event)
    bundle = Bundle(user_id=client.user_id, name="B", event_id=event.event_id, status="draft",
                    created_at=now, updated_at=now)
    db.add(bundle); db.commit(); db.refresh(bundle)

    booking = Booking(
        user_id=client.user_id, vendor_id=vendor.vendor_id, service_id=svc.service_id,
        time_start="18:00", time_end="23:00", location="1 Main", date_iso=date_iso,
        status="payment_confirmed", payment_status="paid",
        amount_cents=500000, platform_fee_cents=25000, bundle_id=bundle.bundle_id,
        venue_latitude=VLAT, venue_longitude=VLNG,
    )
    db.add(booking); db.commit(); db.refresh(booking)
    return db, client, vuser, booking


def test_early_checkin_confirms_vendor_side_without_releasing():
    # Event is in the future — the vendor checks in early (setup / day 1).
    db, _client, vuser, booking = _seed_paid_venue_booking("2099-01-01")

    resp = check_in(booking_id=booking.booking_id, caller_user_id=vuser.user_id,
                    latitude=VLAT, longitude=VLNG, db=db)
    db.refresh(booking)

    assert booking.vendor_checked_in_at is not None
    assert booking.vendor_confirmed_at is not None   # the fix — not stranded
    assert booking.payment_status == "paid"          # customer hasn't confirmed → held
    assert resp["funds_released"] is False
    db.close()


def test_a_refused_transfer_doesnt_report_the_confirmation_as_failed(mocker):
    """The confirmation happened. Only the payout didn't.

    The stranding shape: the vendor's check-in confirms their side, the client
    confirms half a minute later, and Stripe refuses the transfer for a reason
    that belongs to neither of them — an insufficient platform balance. The
    client's confirmation is committed before the transfer is attempted, so
    raising told them it hadn't registered, and their one obvious move — press
    it again — was met with "already confirmed".
    """
    import stripe as stripe_sdk

    mocker.patch(
        "app.services.stripe_service.stripe.Transfer.create",
        side_effect=stripe_sdk.StripeError("insufficient available funds"),
    )
    db, client, vuser, booking = _seed_paid_venue_booking("2000-01-01", stripe_ready=True)

    check_in(booking_id=booking.booking_id, caller_user_id=vuser.user_id,
             latitude=VLAT, longitude=VLNG, db=db)

    resp = confirm_event(booking_id=booking.booking_id, caller_user_id=client.user_id, db=db)
    db.refresh(booking)

    assert resp["funds_released"] is False
    assert booking.customer_confirmed_at is not None  # recorded, and said to be
    assert booking.payment_status == "paid"           # still escrow, for the sweep
    db.close()


def test_the_sweep_pays_out_what_a_refused_transfer_left_behind(mocker):
    """What makes "we'll keep retrying it" a promise rather than a hope."""
    import stripe as stripe_sdk
    from app.services.stripe_service import auto_release_due

    transfer = mocker.patch(
        "app.services.stripe_service.stripe.Transfer.create",
        side_effect=stripe_sdk.StripeError("insufficient available funds"),
    )
    db, client, vuser, booking = _seed_paid_venue_booking("2000-01-01", stripe_ready=True)

    check_in(booking_id=booking.booking_id, caller_user_id=vuser.user_id,
             latitude=VLAT, longitude=VLNG, db=db)
    confirm_event(booking_id=booking.booking_id, caller_user_id=client.user_id, db=db)
    db.refresh(booking)
    assert booking.payment_status == "paid"

    # Next sweep, the balance covers it.
    transfer.side_effect = None
    assert booking.booking_id in auto_release_due(db=db)["released"]
    db.refresh(booking)
    assert booking.payment_status == "released"
    db.close()


def test_early_vendor_checkin_then_customer_confirms_releases(mocker):
    mocker.patch("app.services.stripe_service.stripe.Transfer.create", return_value=object())
    # Past event so the customer is allowed to confirm.
    db, client, vuser, booking = _seed_paid_venue_booking("2000-01-01", stripe_ready=True)

    check_in(booking_id=booking.booking_id, caller_user_id=vuser.user_id,
             latitude=VLAT, longitude=VLNG, db=db)
    db.refresh(booking)
    assert booking.vendor_confirmed_at is not None

    # Customer confirms after the event → both in → release.
    confirm_event(booking_id=booking.booking_id, caller_user_id=client.user_id, db=db)
    db.refresh(booking)
    assert booking.payment_status == "released"
    db.close()
