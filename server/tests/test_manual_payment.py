"""Phase 2 of "optional escrow": a manual-track booking skips Stripe
entirely — no card charge, no held funds — and gets a self-reported
two-sided attestation instead (mark_booking_paid / confirm_payment_received).
Cancelling one is just a status change, since Jorna never held the money.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Booking, Bundle, Service, User, Vendor
from app.services.stripe_service import (
    StripeError,
    cancel_booking,
    cancellation_preview,
    confirm_payment_received,
    mark_booking_paid,
)
from tests.test_api import TestingSessionLocal


def _setup(db, *, status="approved", payment_status="unpaid", date_iso="2027-06-20"):
    uid = uuid.uuid4().hex[:8]
    client = User(
        email=f"mp_c_{uid}@test.com", username=f"mp_c_{uid}", password="pw",
        phone="1", f_name="C", l_name="L", age=30, location="NJ",
        gender="F", language="EN", token_version=0,
    )
    vu = User(
        email=f"mp_v_{uid}@test.com", username=f"mp_v_{uid}", password="pw",
        phone="1", f_name="V", l_name="N", age=30, location="NJ",
        gender="F", language="EN", token_version=0,
    )
    db.add_all([client, vu]); db.commit(); db.refresh(client); db.refresh(vu)
    v = Vendor(
        user_id=vu.user_id, bio="b", category="venue", rating=4.0, num_events=1,
        payment_method="manual", venmo_handle="@vendor",
    )
    db.add(v); db.commit(); db.refresh(v)
    svc = Service(
        name=f"svc-{uid}", price=1000.0, price_unit="event",
        vendor_id=v.vendor_id, experience="e", category="venue", negotiable=False,
    )
    db.add(svc); db.commit(); db.refresh(svc)
    now = datetime.now(timezone.utc)
    bundle = Bundle(
        user_id=client.user_id, name="Plan", status="confirmed",
        created_at=now, updated_at=now,
    )
    db.add(bundle); db.commit(); db.refresh(bundle)
    bk = Booking(
        user_id=client.user_id, vendor_id=v.vendor_id, service_id=svc.service_id,
        date_iso=date_iso, time_start="18:00", time_end="23:00",
        location="12 Maple Ave, Evanston, IL 60201", status=status,
        payment_status=payment_status, amount_cents=100_000,
        payment_method="manual",
        confirmed_at=now,
        bundle_id=bundle.bundle_id,
    )
    db.add(bk); db.commit(); db.refresh(bk)
    return {"client": client, "vendor_user": vu, "vendor": v, "service": svc,
            "bundle": bundle, "booking": bk}


def _teardown(db, s):
    db.query(Booking).filter(Booking.booking_id == s["booking"].booking_id).delete()
    db.query(Bundle).filter(Bundle.bundle_id == s["bundle"].bundle_id).delete()
    db.query(Service).filter(Service.service_id == s["service"].service_id).delete()
    db.query(Vendor).filter(Vendor.vendor_id == s["vendor"].vendor_id).delete()
    db.query(User).filter(
        User.user_id.in_([s["client"].user_id, s["vendor_user"].user_id])
    ).delete(synchronize_session=False)
    db.commit()


class TestMarkBookingPaid:
    def test_client_marks_it_paid(self):
        db = TestingSessionLocal()
        s = _setup(db)
        try:
            result = mark_booking_paid(
                booking_id=s["booking"].booking_id, caller_user_id=s["client"].user_id, db=db,
            )
            assert result["payment_status"] == "marked_paid"
            db.refresh(s["booking"])
            assert s["booking"].payment_status == "marked_paid"
            assert s["booking"].manual_payment_marked_at is not None
        finally:
            _teardown(db, s); db.close()

    def test_only_the_client_can_mark_it_paid(self):
        db = TestingSessionLocal()
        s = _setup(db)
        try:
            with pytest.raises(StripeError) as caught:
                mark_booking_paid(
                    booking_id=s["booking"].booking_id,
                    caller_user_id=s["vendor_user"].user_id, db=db,
                )
            assert caught.value.status_code == 403
        finally:
            _teardown(db, s); db.close()

    def test_rejects_a_stripe_track_booking(self):
        db = TestingSessionLocal()
        s = _setup(db)
        try:
            s["booking"].payment_method = "stripe"
            db.commit()
            with pytest.raises(StripeError) as caught:
                mark_booking_paid(
                    booking_id=s["booking"].booking_id, caller_user_id=s["client"].user_id, db=db,
                )
            assert caught.value.status_code == 400
        finally:
            _teardown(db, s); db.close()

    def test_rejects_marking_twice(self):
        db = TestingSessionLocal()
        s = _setup(db)
        try:
            mark_booking_paid(booking_id=s["booking"].booking_id, caller_user_id=s["client"].user_id, db=db)
            with pytest.raises(StripeError) as caught:
                mark_booking_paid(
                    booking_id=s["booking"].booking_id, caller_user_id=s["client"].user_id, db=db,
                )
            assert caught.value.status_code == 400
        finally:
            _teardown(db, s); db.close()

    def test_rejects_before_the_vendor_accepts(self):
        db = TestingSessionLocal()
        s = _setup(db, status="pending")
        try:
            with pytest.raises(StripeError) as caught:
                mark_booking_paid(
                    booking_id=s["booking"].booking_id, caller_user_id=s["client"].user_id, db=db,
                )
            assert caught.value.status_code == 400
        finally:
            _teardown(db, s); db.close()


class TestConfirmPaymentReceived:
    def test_vendor_confirms_after_the_client_marks_it_paid(self):
        db = TestingSessionLocal()
        s = _setup(db, payment_status="marked_paid")
        try:
            result = confirm_payment_received(
                booking_id=s["booking"].booking_id, caller_user_id=s["vendor_user"].user_id, db=db,
            )
            assert result["payment_status"] == "confirmed_paid"
            db.refresh(s["booking"])
            assert s["booking"].payment_status == "confirmed_paid"
            assert s["booking"].manual_payment_confirmed_at is not None
        finally:
            _teardown(db, s); db.close()

    def test_only_the_vendor_can_confirm(self):
        db = TestingSessionLocal()
        s = _setup(db, payment_status="marked_paid")
        try:
            with pytest.raises(StripeError) as caught:
                confirm_payment_received(
                    booking_id=s["booking"].booking_id, caller_user_id=s["client"].user_id, db=db,
                )
            assert caught.value.status_code == 403
        finally:
            _teardown(db, s); db.close()

    def test_rejects_confirming_before_the_client_marks_it_paid(self):
        db = TestingSessionLocal()
        s = _setup(db, payment_status="unpaid")
        try:
            with pytest.raises(StripeError) as caught:
                confirm_payment_received(
                    booking_id=s["booking"].booking_id, caller_user_id=s["vendor_user"].user_id, db=db,
                )
            assert caught.value.status_code == 400
        finally:
            _teardown(db, s); db.close()


class TestCancelManualBooking:
    def test_cancelling_is_a_plain_status_change_no_stripe_calls(self, mocker):
        refund = mocker.patch("stripe.Refund.create")
        transfer = mocker.patch("stripe.Transfer.create")
        db = TestingSessionLocal()
        s = _setup(db, payment_status="confirmed_paid")
        try:
            result = cancel_booking(
                booking_id=s["booking"].booking_id, caller_user_id=s["client"].user_id, db=db,
            )
            refund.assert_not_called()
            transfer.assert_not_called()
            assert result["refund_cents"] == 0
            assert result["vendor_cancellation_cents"] == 0
            db.refresh(s["booking"])
            assert s["booking"].status == "rejected"
            assert s["booking"].cancelled_at is not None
            # payment_status is left as a historical fact, not overwritten —
            # Jorna never held this money and has no authority to change it.
            assert s["booking"].payment_status == "confirmed_paid"
        finally:
            _teardown(db, s); db.close()

    def test_cancellable_even_when_never_marked_paid(self, mocker):
        mocker.patch("stripe.Refund.create")
        mocker.patch("stripe.Transfer.create")
        db = TestingSessionLocal()
        s = _setup(db, payment_status="unpaid")
        try:
            result = cancel_booking(
                booking_id=s["booking"].booking_id, caller_user_id=s["client"].user_id, db=db,
            )
            assert result["refund_cents"] == 0
            db.refresh(s["booking"])
            assert s["booking"].status == "rejected"
        finally:
            _teardown(db, s); db.close()


class TestCancellationPreviewForManual:
    def test_returns_none_for_a_manual_booking(self):
        db = TestingSessionLocal()
        s = _setup(db, payment_status="confirmed_paid")
        try:
            preview = cancellation_preview(
                booking_id=s["booking"].booking_id, caller_user_id=s["client"].user_id, db=db,
            )
            assert preview is None
        finally:
            _teardown(db, s); db.close()
