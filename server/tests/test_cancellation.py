"""The client cancellation & refund-split policy.

cancellation_split is pure: full refund for GRACE_HOURS after the vendor
accepts, then nothing back to the client — the payment splits between the
platform and the vendor on a linear ramp instead, from 99%/1% right after
grace to 1%/99% by the day before the event. cancel_booking is the action
that actually moves the money.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_chatbot.db")

from app.db.models import Booking, Bundle, Service, User, Vendor
from app.services.stripe_service import GRACE_HOURS, StripeError, cancel_booking, cancellation_split
from tests.test_api import TestingSessionLocal


def _booking(**overrides) -> Booking:
    defaults = dict(
        date_iso="2027-06-20",
        time_start="18:00",
        time_end="23:00",
        location="12 Maple Ave, Evanston, IL 60201",
        status="approved",
        payment_status="paid",
        amount_cents=100_000,
        currency="usd",
    )
    defaults.update(overrides)
    return Booking(**defaults)


# ── cancellation_split: pure math, no DB ────────────────────────────────


class TestCancellationSplit:
    def test_never_accepted_is_a_full_refund(self):
        b = _booking(confirmed_at=None)
        now = datetime.now(timezone.utc)
        split = cancellation_split(b, now)
        assert split == {"refund_to_client_cents": 100_000, "vendor_cents": 0, "platform_cents": 0}

    def test_within_grace_is_a_full_refund(self):
        now = datetime.now(timezone.utc)
        b = _booking(confirmed_at=now - timedelta(hours=GRACE_HOURS - 1))
        split = cancellation_split(b, now)
        assert split["refund_to_client_cents"] == 100_000
        assert split["vendor_cents"] == 0

    def test_exactly_at_grace_end_starts_the_ramp(self):
        now = datetime.now(timezone.utc)
        confirmed_at = now - timedelta(hours=GRACE_HOURS)
        b = _booking(confirmed_at=confirmed_at, date_iso="2099-01-01")
        split = cancellation_split(b, now)
        assert split["refund_to_client_cents"] == 0
        assert split["vendor_cents"] == 1_000  # 1% of 100,000

    def test_midway_through_the_ramp(self):
        confirmed_at = datetime(2027, 6, 1, tzinfo=timezone.utc)
        t0 = confirmed_at + timedelta(hours=GRACE_HOURS)
        t1 = datetime(2027, 6, 19, tzinfo=timezone.utc)  # day before the event
        midpoint = t0 + (t1 - t0) / 2
        b = _booking(confirmed_at=confirmed_at, date_iso="2027-06-20")
        split = cancellation_split(b, midpoint)
        assert split["refund_to_client_cents"] == 0
        # ~50% of the way from 1% to 99% is ~50%
        assert 48_000 <= split["vendor_cents"] <= 52_000

    def test_on_the_day_before_the_event_vendor_gets_99_percent(self):
        confirmed_at = datetime(2027, 6, 1, tzinfo=timezone.utc)
        b = _booking(confirmed_at=confirmed_at, date_iso="2027-06-20")
        t1 = datetime(2027, 6, 19, tzinfo=timezone.utc)
        split = cancellation_split(b, t1)
        assert split["vendor_cents"] == 99_000
        assert split["platform_cents"] == 1_000

    def test_past_the_day_before_stays_capped_at_99_percent(self):
        confirmed_at = datetime(2027, 6, 1, tzinfo=timezone.utc)
        b = _booking(confirmed_at=confirmed_at, date_iso="2027-06-20")
        way_past = datetime(2027, 6, 20, 10, tzinfo=timezone.utc)
        split = cancellation_split(b, way_past)
        assert split["vendor_cents"] == 99_000

    def test_last_minute_booking_jumps_straight_to_99_percent(self):
        """Accepted so close to the event that grace-end lands on/after the
        day before — no room for the ramp to run."""
        confirmed_at = datetime(2027, 6, 18, 10, tzinfo=timezone.utc)
        b = _booking(confirmed_at=confirmed_at, date_iso="2027-06-19")
        t0 = confirmed_at + timedelta(hours=GRACE_HOURS)
        split = cancellation_split(b, t0 + timedelta(minutes=1))
        assert split["refund_to_client_cents"] == 0
        assert split["vendor_cents"] == 99_000

    def test_tbd_date_never_starts_the_ramp(self):
        confirmed_at = datetime(2027, 6, 1, tzinfo=timezone.utc)
        b = _booking(confirmed_at=confirmed_at, date_iso="TBD")
        way_later = confirmed_at + timedelta(days=90)
        split = cancellation_split(b, way_later)
        # No event date to ramp toward — clamps to the vendor-favoring end,
        # same as a last-minute booking, rather than erroring.
        assert split["vendor_cents"] == 99_000


# ── cancel_booking: the real action, DB + Stripe ────────────────────────


class TestCancelBooking:
    def _setup(self, db, *, confirmed_hours_ago=1, date_iso="2027-06-20", amount_cents=100_000):
        uid = uuid.uuid4().hex[:8]
        client = User(
            email=f"cxl_c_{uid}@test.com", username=f"cxl_c_{uid}", password="pw",
            phone="1", f_name="C", l_name="L", age=30, location="NJ",
            gender="F", language="EN", token_version=0,
        )
        vu = User(
            email=f"cxl_v_{uid}@test.com", username=f"cxl_v_{uid}", password="pw",
            phone="1", f_name="V", l_name="N", age=30, location="NJ",
            gender="F", language="EN", token_version=0,
        )
        db.add_all([client, vu]); db.commit(); db.refresh(client); db.refresh(vu)
        v = Vendor(
            user_id=vu.user_id, bio="b", category="venue", rating=4.0, num_events=1,
            stripe_account_id="acct_test_123",
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
            location="12 Maple Ave, Evanston, IL 60201", status="approved",
            payment_status="paid", amount_cents=amount_cents,
            payment_intent_id=f"pi_{uid}",
            confirmed_at=now - timedelta(hours=confirmed_hours_ago),
            bundle_id=bundle.bundle_id,
        )
        db.add(bk); db.commit(); db.refresh(bk)
        return {"client": client, "vendor_user": vu, "vendor": v, "service": svc,
                "bundle": bundle, "booking": bk}

    def _teardown(self, db, s):
        from app.db.models import Booking, Bundle, Service, User, Vendor

        db.query(Booking).filter(Booking.booking_id == s["booking"].booking_id).delete()
        db.query(Bundle).filter(Bundle.bundle_id == s["bundle"].bundle_id).delete()
        db.query(Service).filter(Service.service_id == s["service"].service_id).delete()
        db.query(Vendor).filter(Vendor.vendor_id == s["vendor"].vendor_id).delete()
        db.query(User).filter(
            User.user_id.in_([s["client"].user_id, s["vendor_user"].user_id])
        ).delete(synchronize_session=False)
        db.commit()

    def test_within_grace_refunds_in_full(self, mocker):
        refund = mocker.patch("stripe.Refund.create")
        transfer = mocker.patch("stripe.Transfer.create")
        db = TestingSessionLocal()
        s = self._setup(db, confirmed_hours_ago=1)
        try:
            result = cancel_booking(
                booking_id=s["booking"].booking_id, caller_user_id=s["client"].user_id, db=db,
            )
            refund.assert_called_once()
            transfer.assert_not_called()
            assert result["refund_cents"] == 100_000
            assert result["vendor_cancellation_cents"] == 0
            db.refresh(s["booking"])
            assert s["booking"].payment_status == "refunded"
            assert s["booking"].status == "rejected"
            assert s["booking"].refund_cents == 100_000
            assert s["booking"].cancelled_at is not None
        finally:
            self._teardown(db, s); db.close()

    def test_past_grace_pays_the_vendor_their_share(self, mocker):
        refund = mocker.patch("stripe.Refund.create")
        transfer = mocker.patch("stripe.Transfer.create")
        db = TestingSessionLocal()
        # Confirmed just past grace, far from the event — near the 1% end.
        s = self._setup(
            db, confirmed_hours_ago=GRACE_HOURS + 1, date_iso="2099-01-01",
        )
        try:
            result = cancel_booking(
                booking_id=s["booking"].booking_id, caller_user_id=s["client"].user_id, db=db,
            )
            refund.assert_not_called()
            transfer.assert_called_once()
            assert transfer.call_args.kwargs["destination"] == "acct_test_123"
            assert result["refund_cents"] == 0
            assert result["vendor_cancellation_cents"] > 0
            db.refresh(s["booking"])
            assert s["booking"].payment_status == "cancelled"
            assert s["booking"].status == "rejected"
            assert s["booking"].vendor_cancellation_cents == result["vendor_cancellation_cents"]
        finally:
            self._teardown(db, s); db.close()

    def test_only_the_client_can_cancel(self, mocker):
        mocker.patch("stripe.Refund.create")
        db = TestingSessionLocal()
        s = self._setup(db)
        try:
            with pytest.raises(StripeError) as caught:
                cancel_booking(
                    booking_id=s["booking"].booking_id,
                    caller_user_id=s["vendor_user"].user_id, db=db,
                )
            assert caught.value.status_code == 403
        finally:
            self._teardown(db, s); db.close()

    def test_already_released_cannot_be_cancelled(self, mocker):
        db = TestingSessionLocal()
        s = self._setup(db)
        s["booking"].payment_status = "released"
        db.commit()
        try:
            with pytest.raises(StripeError) as caught:
                cancel_booking(
                    booking_id=s["booking"].booking_id,
                    caller_user_id=s["client"].user_id, db=db,
                )
            assert caught.value.status_code == 400
        finally:
            self._teardown(db, s); db.close()

    def test_cannot_cancel_after_the_event_has_happened(self, mocker):
        db = TestingSessionLocal()
        s = self._setup(db, date_iso="2020-01-01")
        try:
            with pytest.raises(StripeError) as caught:
                cancel_booking(
                    booking_id=s["booking"].booking_id,
                    caller_user_id=s["client"].user_id, db=db,
                )
            assert caught.value.status_code == 400
        finally:
            self._teardown(db, s); db.close()
