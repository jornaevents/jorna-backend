"""Moving a booking that has already been agreed.

The test list from RESCHEDULE_PROPOSAL.md. Two properties run through all of it:

  Escrow does not move on a proposal, only on a resolution.
  A proposal is plan-wide; an answer is per vendor.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_chatbot.db")

from app.db.models import Booking, Bundle, ChangeRequest, Service, User, Vendor
from app.models.schemas import BookingStatus
from app.services import change_request_service as crs
from app.services.change_request_service import ChangeRequestError
from tests.test_api import TestingSessionLocal


NEW_DATE = "2027-06-14"
OLD_DATE = "2027-06-05"


@pytest.fixture
def plan():
    """A sent plan: one client, two vendors, two paid-up approved bookings."""
    db = TestingSessionLocal()
    uid = uuid.uuid4().hex[:8]

    client = User(
        email=f"cr_client_{uid}@test.com", username=f"cr_client_{uid}", password="pw",
        phone="1", f_name="Cli", l_name="Ent", age=30, location="NJ",
        gender="F", language="EN", token_version=0,
    )
    db.add(client); db.commit(); db.refresh(client)

    now = datetime.now(timezone.utc)
    bundle = Bundle(
        user_id=client.user_id, name="Wedding", event_name="Wedding",
        status="confirmed", created_at=now, updated_at=now,
    )
    db.add(bundle); db.commit(); db.refresh(bundle)

    vendors, bookings = [], []
    for n, (price, unit) in enumerate([(8000.0, "event"), (500.0, "day")]):
        vu = User(
            email=f"cr_v{n}_{uid}@test.com", username=f"cr_v{n}_{uid}", password="pw",
            phone="1", f_name=f"Vend{n}", l_name="Or", age=30, location="NJ",
            gender="F", language="EN", token_version=0,
        )
        db.add(vu); db.commit(); db.refresh(vu)
        v = Vendor(user_id=vu.user_id, bio="b", category="venue", rating=4.5, num_events=3)
        db.add(v); db.commit(); db.refresh(v)
        svc = Service(
            name=f"Service {n}", price=price, price_unit=unit, vendor_id=v.vendor_id,
            experience="exp", category="venue", negotiable=False,
        )
        db.add(svc); db.commit(); db.refresh(svc)
        bk = Booking(
            user_id=client.user_id, vendor_id=v.vendor_id, service_id=svc.service_id,
            date_iso=OLD_DATE, time_start="18:00", time_end="23:00",
            location="12 Maple Ave, Evanston, IL 60201",
            status=BookingStatus.APPROVED.value, payment_status="paid",
            amount_cents=int(price * 100), paid_at=now,
            payment_intent_id=f"pi_{uid}_{n}", bundle_id=bundle.bundle_id,
        )
        db.add(bk); db.commit(); db.refresh(bk)
        vendors.append((v, vu, svc)); bookings.append(bk)

    yield {
        "db": db, "client": client, "bundle": bundle,
        "vendors": vendors, "bookings": bookings,
    }

    # Tear the whole plan down. The test DB is shared across modules, and these
    # vendors and services are real rows that /vendors/search will happily
    # return — leaving them behind broke test_service_detail, which asserts on
    # what a search finds. Reverse order, so no foreign key is orphaned.
    db.rollback()
    booking_ids = [b.booking_id for b in bookings]
    db.query(ChangeRequest).filter(
        ChangeRequest.booking_id.in_(booking_ids)
    ).delete(synchronize_session=False)
    db.query(Booking).filter(Booking.bundle_id == bundle.bundle_id).delete(
        synchronize_session=False
    )
    db.query(Booking).filter(Booking.booking_id.in_(booking_ids)).delete(
        synchronize_session=False
    )
    db.query(Bundle).filter(Bundle.bundle_id == bundle.bundle_id).delete(
        synchronize_session=False
    )
    for v, vu, svc in vendors:
        db.query(Service).filter(Service.service_id == svc.service_id).delete(
            synchronize_session=False
        )
        db.query(Vendor).filter(Vendor.vendor_id == v.vendor_id).delete(
            synchronize_session=False
        )
        db.query(User).filter(User.user_id == vu.user_id).delete(
            synchronize_session=False
        )
    db.query(User).filter(User.user_id == client.user_id).delete(
        synchronize_session=False
    )
    db.commit()
    db.close()


def _propose(plan, **over):
    args = dict(
        bundle_id=plan["bundle"].bundle_id,
        caller_user_id=plan["client"].user_id,
        date_iso=NEW_DATE, date_end=None, time_start=None, time_end=None,
        message=None, db=plan["db"],
    )
    args.update(over)
    return crs.propose(**args)


def _requests(plan):
    return (
        plan["db"].query(ChangeRequest)
        .filter(ChangeRequest.booking_id.in_([b.booking_id for b in plan["bookings"]]))
        .all()
    )


# ── Proposing ─────────────────────────────────────────────────────────


def test_one_request_per_agreed_booking(plan):
    result = _propose(plan)
    assert result["count"] == 2
    assert {r.booking_id for r in _requests(plan)} == {
        b.booking_id for b in plan["bookings"]
    }


def test_dead_bookings_are_skipped(plan):
    """A declined booking isn't going ahead whatever date it names."""
    db = plan["db"]
    plan["bookings"][0].status = BookingStatus.REJECTED.value
    db.commit()

    assert _propose(plan)["count"] == 1
    assert {r.booking_id for r in _requests(plan)} == {
        plan["bookings"][1].booking_id
    }


def test_escrow_does_not_move_on_a_proposal(plan):
    """The property the whole design rests on."""
    db = plan["db"]
    before = [(b.payment_status, b.amount_cents, b.date_iso) for b in plan["bookings"]]
    _propose(plan)
    for bk in plan["bookings"]:
        db.refresh(bk)
    after = [(b.payment_status, b.amount_cents, b.date_iso) for b in plan["bookings"]]
    assert before == after


def test_a_draft_cannot_be_proposed_against(plan):
    """Its details are still directly editable and no vendor was told anything."""
    db = plan["db"]
    plan["bundle"].status = "draft"
    db.commit()
    with pytest.raises(ChangeRequestError) as caught:
        _propose(plan)
    assert caught.value.status_code == 400
    assert "hasn't been sent" in caught.value.detail


def test_a_proposal_must_say_what_it_moves_to(plan):
    with pytest.raises(ChangeRequestError) as caught:
        _propose(plan, date_iso=None)
    assert caught.value.status_code == 400


def test_only_one_proposal_at_a_time(plan):
    """Two live sets of proposed dates is a question with two answers."""
    _propose(plan)
    with pytest.raises(ChangeRequestError) as caught:
        _propose(plan, date_iso="2027-07-01")
    assert caught.value.status_code == 409


def test_someone_elses_plan_is_refused(plan):
    with pytest.raises(ChangeRequestError) as caught:
        _propose(plan, caller_user_id="not-the-owner")
    assert caught.value.status_code == 403


# ── Answering ─────────────────────────────────────────────────────────


def _answer(plan, index, accept, message=None):
    cr = next(
        r for r in _requests(plan)
        if r.booking_id == plan["bookings"][index].booking_id
    )
    _, vendor_user, _ = plan["vendors"][index]
    return crs.respond(
        change_request_id=cr.change_request_id,
        caller_user_id=vendor_user.user_id,
        accept=accept, message=message, db=plan["db"],
    )


def test_accept_moves_the_booking(plan):
    db = plan["db"]
    _propose(plan)
    _answer(plan, 0, accept=True)
    db.refresh(plan["bookings"][0])
    assert plan["bookings"][0].date_iso == NEW_DATE


def test_decline_leaves_it_alone(plan):
    db = plan["db"]
    _propose(plan)
    _answer(plan, 0, accept=False, message="Already booked that weekend")
    db.refresh(plan["bookings"][0])
    assert plan["bookings"][0].date_iso == OLD_DATE
    assert plan["bookings"][0].payment_status == "paid"


def test_one_vendors_answer_does_not_touch_another(plan):
    """Per-vendor answers. The point of the chosen scope."""
    db = plan["db"]
    _propose(plan)
    _answer(plan, 0, accept=True)
    db.refresh(plan["bookings"][1])
    assert plan["bookings"][1].date_iso == OLD_DATE, "the other vendor was moved too"


def test_a_vendor_cannot_answer_another_vendors_request(plan):
    _propose(plan)
    cr = next(
        r for r in _requests(plan)
        if r.booking_id == plan["bookings"][0].booking_id
    )
    _, wrong_vendor_user, _ = plan["vendors"][1]
    with pytest.raises(ChangeRequestError) as caught:
        crs.respond(
            change_request_id=cr.change_request_id,
            caller_user_id=wrong_vendor_user.user_id,
            accept=True, message=None, db=plan["db"],
        )
    assert caught.value.status_code == 403


def test_the_client_cannot_accept_their_own_proposal(plan):
    _propose(plan)
    cr = _requests(plan)[0]
    with pytest.raises(ChangeRequestError) as caught:
        crs.respond(
            change_request_id=cr.change_request_id,
            caller_user_id=plan["client"].user_id,
            accept=True, message=None, db=plan["db"],
        )
    assert caught.value.status_code == 403


def test_answering_twice_is_refused(plan):
    _propose(plan)
    _answer(plan, 0, accept=False)
    with pytest.raises(ChangeRequestError) as caught:
        _answer(plan, 0, accept=True)
    assert caught.value.status_code == 400


# ── The conflict re-check ─────────────────────────────────────────────


def test_accepting_into_a_clash_is_refused(plan):
    """A vendor accepting a date they're already booked on is a double-booking.

    The same guard approval uses. Nothing else in the app lets a vendor be in
    two places, and this must not be the exception.
    """
    db = plan["db"]
    vendor, _, svc = plan["vendors"][0]
    clash = Booking(
        user_id="someone-else", vendor_id=vendor.vendor_id, service_id=svc.service_id,
        date_iso=NEW_DATE, time_start="18:00", time_end="23:00", location="Elsewhere",
        status=BookingStatus.APPROVED.value,
    )
    db.add(clash); db.commit()

    _propose(plan)
    try:
        with pytest.raises(ChangeRequestError) as caught:
            _answer(plan, 0, accept=True)
        assert caught.value.status_code == 409
        db.refresh(plan["bookings"][0])
        assert plan["bookings"][0].date_iso == OLD_DATE, "moved into a clash"
    finally:
        db.delete(clash); db.commit()


def test_the_bookings_own_dates_are_not_a_clash_with_itself(plan):
    """It is the one booking that must be excluded from its own check."""
    _propose(plan, date_iso=None, time_start="19:00", time_end="23:30")
    _answer(plan, 0, accept=True)
    plan["db"].refresh(plan["bookings"][0])
    assert plan["bookings"][0].time_start == "19:00"


# ── Re-pricing ────────────────────────────────────────────────────────


def test_a_shorter_booking_refunds_the_difference(plan, mocker):
    """The per-day booking moves from 3 days to 1."""
    db = plan["db"]
    refund = mocker.patch("stripe.Refund.create")
    per_day = plan["bookings"][1]
    per_day.date_iso, per_day.date_end = OLD_DATE, "2027-06-07"
    per_day.amount_cents = 150_000  # 3 days at $500
    db.commit()

    _propose(plan, date_iso=NEW_DATE, date_end=NEW_DATE)
    _answer(plan, 1, accept=True)
    db.refresh(per_day)

    assert per_day.amount_cents == 50_000, "not re-priced to one day"
    refund.assert_called_once()
    assert refund.call_args.kwargs["amount"] == 100_000


def test_a_longer_booking_waits_for_consent_and_charges_nothing(plan, mocker):
    """Nobody should be charged more by a flow they started to fix a date."""
    db = plan["db"]
    charge = mocker.patch("stripe.PaymentIntent.create")
    per_day = plan["bookings"][1]
    per_day.date_end = None
    per_day.amount_cents = 50_000  # one day
    db.commit()

    _propose(plan, date_iso=NEW_DATE, date_end="2027-06-16")  # three days
    result = _answer(plan, 1, accept=True)
    db.refresh(per_day)

    assert result["repriced_amount_cents"] == 150_000
    assert per_day.date_iso == OLD_DATE, "moved before the client agreed to pay more"
    assert per_day.amount_cents == 50_000
    charge.assert_not_called()


def test_consent_completes_the_move(plan):
    db = plan["db"]
    per_day = plan["bookings"][1]
    per_day.date_end = None
    per_day.amount_cents = 50_000
    db.commit()

    _propose(plan, date_iso=NEW_DATE, date_end="2027-06-16")
    _answer(plan, 1, accept=True)
    cr = next(r for r in _requests(plan) if r.booking_id == per_day.booking_id)

    crs.consent(
        change_request_id=cr.change_request_id,
        caller_user_id=plan["client"].user_id, db=db,
    )
    db.refresh(per_day); db.refresh(cr)
    assert per_day.date_iso == NEW_DATE
    assert per_day.amount_cents == 150_000
    assert cr.status == "accepted"


def test_only_the_client_can_consent(plan):
    db = plan["db"]
    per_day = plan["bookings"][1]
    per_day.date_end = None
    per_day.amount_cents = 50_000
    db.commit()
    _propose(plan, date_iso=NEW_DATE, date_end="2027-06-16")
    _answer(plan, 1, accept=True)
    cr = next(r for r in _requests(plan) if r.booking_id == per_day.booking_id)

    _, vendor_user, _ = plan["vendors"][1]
    with pytest.raises(ChangeRequestError) as caught:
        crs.consent(
            change_request_id=cr.change_request_id,
            caller_user_id=vendor_user.user_id, db=db,
        )
    assert caught.value.status_code == 403


def test_consent_on_a_request_that_isnt_waiting_is_refused(plan):
    _propose(plan)
    cr = _requests(plan)[0]
    with pytest.raises(ChangeRequestError) as caught:
        crs.consent(
            change_request_id=cr.change_request_id,
            caller_user_id=plan["client"].user_id, db=plan["db"],
        )
    assert caught.value.status_code == 400


# ── Expiry ────────────────────────────────────────────────────────────


def test_expires_after_the_window_and_not_before(plan):
    db = plan["db"]
    _propose(plan)
    requests = _requests(plan)

    # One day short: still theirs to answer.
    for cr in requests:
        cr.created_at = datetime.now(timezone.utc) - timedelta(
            days=crs.RESPONSE_WINDOW_DAYS - 1
        )
    db.commit()
    assert crs.expire_due(db=db)["expired"] == []

    # Past it.
    for cr in requests:
        cr.created_at = datetime.now(timezone.utc) - timedelta(
            days=crs.RESPONSE_WINDOW_DAYS, hours=1
        )
    db.commit()
    assert len(crs.expire_due(db=db)["expired"]) == 2
    for cr in requests:
        db.refresh(cr)
        assert cr.status == "expired"


def test_reading_a_stale_request_expires_it(plan):
    """A read must never report a live request that is out of time."""
    db = plan["db"]
    _propose(plan)
    for cr in _requests(plan):
        cr.created_at = datetime.now(timezone.utc) - timedelta(
            days=crs.RESPONSE_WINDOW_DAYS + 1
        )
    db.commit()

    board = crs.for_bundle(
        bundle_id=plan["bundle"].bundle_id,
        caller_user_id=plan["client"].user_id, db=db,
    )
    assert {r["status"] for r in board["requests"]} == {"expired"}


def test_an_expired_request_cannot_be_answered(plan):
    db = plan["db"]
    _propose(plan)
    for cr in _requests(plan):
        cr.created_at = datetime.now(timezone.utc) - timedelta(
            days=crs.RESPONSE_WINDOW_DAYS + 1
        )
    db.commit()
    with pytest.raises(ChangeRequestError) as caught:
        _answer(plan, 0, accept=True)
    assert caught.value.status_code == 400


# ── Withdraw ──────────────────────────────────────────────────────────


def test_withdraw_cancels_every_outstanding_request(plan):
    db = plan["db"]
    _propose(plan)
    _answer(plan, 0, accept=False)  # one already settled

    result = crs.withdraw(
        bundle_id=plan["bundle"].bundle_id,
        caller_user_id=plan["client"].user_id, db=db,
    )
    assert result["withdrawn"] == 1, "withdrew a request that was already answered"
    statuses = {r.status for r in _requests(plan)}
    assert statuses == {"declined", "withdrawn"}


def test_withdrawing_frees_the_plan_to_propose_again(plan):
    _propose(plan)
    crs.withdraw(
        bundle_id=plan["bundle"].bundle_id,
        caller_user_id=plan["client"].user_id, db=plan["db"],
    )
    assert _propose(plan, date_iso="2027-08-01")["count"] == 2


# ── The refund a decline unlocks ──────────────────────────────────────


def test_a_decline_unlocks_a_partial_refund(plan, mocker):
    """10% retained for the date the vendor held. See the spec's fee decision."""
    from app.services.stripe_service import (
        RESCHEDULE_CANCELLATION_PCT,
        refund_after_failed_reschedule,
    )

    db = plan["db"]
    refund = mocker.patch("stripe.Refund.create")
    _propose(plan)
    _answer(plan, 0, accept=False)

    booking = plan["bookings"][0]
    result = refund_after_failed_reschedule(
        booking_id=booking.booking_id,
        caller_user_id=plan["client"].user_id, db=db,
    )
    expected = int(round(800_000 * (100 - RESCHEDULE_CANCELLATION_PCT) / 100))
    assert refund.call_args.kwargs["amount"] == expected
    assert result["refunded_cents"] == expected
    db.refresh(booking)
    assert booking.payment_status == "refunded"


def test_no_declined_request_means_the_ordinary_rules_apply(plan, mocker):
    """This is not a way round the 24-hour window."""
    from app.services.stripe_service import StripeError, refund_after_failed_reschedule

    mocker.patch("stripe.Refund.create")
    with pytest.raises(StripeError) as caught:
        refund_after_failed_reschedule(
            booking_id=plan["bookings"][0].booking_id,
            caller_user_id=plan["client"].user_id, db=plan["db"],
        )
    assert caught.value.status_code == 400
    assert "ordinary refund rules" in caught.value.detail


def test_accepting_does_not_unlock_a_refund(plan, mocker):
    from app.services.stripe_service import StripeError, refund_after_failed_reschedule

    mocker.patch("stripe.Refund.create")
    _propose(plan)
    _answer(plan, 0, accept=True)
    with pytest.raises(StripeError):
        refund_after_failed_reschedule(
            booking_id=plan["bookings"][0].booking_id,
            caller_user_id=plan["client"].user_id, db=plan["db"],
        )
