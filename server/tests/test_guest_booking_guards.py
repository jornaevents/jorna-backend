"""Guard audit for guest/contract bookings (Booking.user_id IS NULL) — see
docs/DECISIONS.md's guest-booking entry. Every feature that assumes a
joinable client User must refuse cleanly (400/404) instead of crashing on
the null join once a real guest-booking-creation endpoint exists (a later
phase). This file locks in that behavior now, against a guest booking
constructed directly at the model layer.

Also confirms (with no code change needed) that several existing
`booking.user_id != caller_user_id`-style checks already fail closed for a
null user_id, since no caller string ever equals None.
"""

import uuid
from datetime import datetime, timezone

from app.db.models import Booking, Service, User, Vendor
from app.services.message_service import MessageError, send_message
from app.services.negotiation_service import NegotiationError, start_negotiation
from app.services.conversation_service import ConversationError, open_booking_thread
from app.services.review_service import ReviewError, create_review
from app.services.stripe_service import StripeError, mark_booking_paid
from tests.test_api import TestingSessionLocal, client, make_auth_headers


def _setup_guest_booking(db, *, status="approved"):
    uid = uuid.uuid4().hex[:8]
    vendor_user = User(
        email=f"guest_v_{uid}@test.com", username=f"guest_v_{uid}", password="pw",
        phone="1", f_name="V", l_name="N", age=30, location="NJ",
        gender="F", language="EN", token_version=0,
    )
    db.add(vendor_user); db.commit(); db.refresh(vendor_user)
    vendor = Vendor(
        user_id=vendor_user.user_id, bio="b", category="venue", rating=4.0, num_events=1,
        payment_method="manual", venmo_handle="@vendor",
    )
    db.add(vendor); db.commit(); db.refresh(vendor)
    service = Service(
        name=f"svc-{uid}", price=1000.0, price_unit="event",
        vendor_id=vendor.vendor_id, experience="e", category="venue", negotiable=True,
    )
    db.add(service); db.commit(); db.refresh(service)
    now = datetime.now(timezone.utc)
    booking = Booking(
        user_id=None,
        guest_name="Priya Guest", guest_email="priya@example.com", guest_phone="5551234",
        contract_token=f"tok_{uid}",
        vendor_id=vendor.vendor_id, service_id=service.service_id,
        date_iso="2027-06-20", time_start="18:00", time_end="23:00",
        location="12 Maple Ave, Evanston, IL 60201", status=status,
        payment_status="unpaid", amount_cents=100_000, payment_method="manual",
        confirmed_at=now,
    )
    db.add(booking); db.commit(); db.refresh(booking)
    return {"vendor_user": vendor_user, "vendor": vendor, "service": service, "booking": booking}


def _teardown(db, s):
    db.query(Booking).filter(Booking.booking_id == s["booking"].booking_id).delete()
    db.query(Service).filter(Service.service_id == s["service"].service_id).delete()
    db.query(Vendor).filter(Vendor.vendor_id == s["vendor"].vendor_id).delete()
    db.query(User).filter(User.user_id == s["vendor_user"].user_id).delete()
    db.commit()


def test_vendor_cannot_open_a_message_thread_on_their_own_guest_booking():
    """Regression: open_booking_thread's `caller_user_id in (booking.user_id,
    vendor_user.user_id)` check used to let the vendor through even when
    booking.user_id is None, then tried to create a ConversationMember with
    user_id=None -- a crash, not a clean refusal."""
    db = TestingSessionLocal()
    s = _setup_guest_booking(db)
    try:
        try:
            open_booking_thread(
                booking_id=s["booking"].booking_id,
                caller_user_id=s["vendor_user"].user_id,
                db=db,
            )
            assert False, "expected ConversationError"
        except ConversationError as e:
            assert e.status_code == 400
    finally:
        _teardown(db, s)
        db.close()


def test_send_message_refused_for_a_guest_booking():
    db = TestingSessionLocal()
    s = _setup_guest_booking(db)
    try:
        try:
            send_message(
                booking_id=s["booking"].booking_id,
                content="hello",
                caller_user_id=s["vendor_user"].user_id,
                db=db,
            )
            assert False, "expected MessageError"
        except MessageError as e:
            assert e.status_code == 400
    finally:
        _teardown(db, s)
        db.close()


def test_start_negotiation_refused_for_a_guest_booking(monkeypatch):
    # New counters are retired outright (docs/DECISIONS.md #23); this checks
    # the guest guard underneath, which still holds if they come back.
    from app.services import negotiation_service

    monkeypatch.setattr(negotiation_service, "NEW_NEGOTIATIONS_OPEN", True)
    db = TestingSessionLocal()
    s = _setup_guest_booking(db, status="pending")
    try:
        try:
            start_negotiation(
                booking_id=s["booking"].booking_id,
                amount_cents=50_000,
                message=None,
                caller_user_id=s["vendor_user"].user_id,
                db=db,
            )
            assert False, "expected NegotiationError"
        except NegotiationError as e:
            assert e.status_code == 400
    finally:
        _teardown(db, s)
        db.close()


def test_client_side_mark_booking_paid_already_fails_closed_for_a_guest_booking():
    """No code change needed here -- booking.user_id != caller_user_id is
    always True when user_id is None, since no real caller id ever equals
    None. Locked in as a regression test, not just reasoned about."""
    db = TestingSessionLocal()
    s = _setup_guest_booking(db)
    try:
        try:
            mark_booking_paid(
                booking_id=s["booking"].booking_id,
                caller_user_id=s["vendor_user"].user_id,
                db=db,
            )
            assert False, "expected StripeError"
        except StripeError as e:
            assert e.status_code == 403
    finally:
        _teardown(db, s)
        db.close()


def test_review_creation_already_fails_closed_for_a_guest_booking():
    """No code change needed -- same None-never-equals-a-real-id reasoning
    as mark_booking_paid above."""
    db = TestingSessionLocal()
    s = _setup_guest_booking(db, status="payment_confirmed")
    try:
        try:
            create_review(
                booking_id=s["booking"].booking_id,
                rating=5,
                comment="great",
                caller_user_id=s["vendor_user"].user_id,
                db=db,
            )
            assert False, "expected ReviewError"
        except ReviewError as e:
            assert e.status_code == 403
    finally:
        _teardown(db, s)
        db.close()


def test_messages_route_returns_400_not_500_for_a_guest_booking():
    """End-to-end through the HTTP layer, not just the service function --
    confirms the router's exception handling actually surfaces this as a
    clean 400 rather than an unhandled crash."""
    db = TestingSessionLocal()
    s = _setup_guest_booking(db)
    try:
        resp = client.post(
            "/messages",
            json={"booking_id": s["booking"].booking_id, "content": "hi"},
            headers=make_auth_headers(s["vendor_user"]),
        )
        assert resp.status_code == 400, resp.text
    finally:
        _teardown(db, s)
        db.close()


def test_open_booking_thread_route_returns_400_not_500_for_a_guest_booking():
    db = TestingSessionLocal()
    s = _setup_guest_booking(db)
    try:
        resp = client.post(
            f"/conversations/booking/{s['booking'].booking_id}",
            headers=make_auth_headers(s["vendor_user"]),
        )
        assert resp.status_code == 400, resp.text
    finally:
        _teardown(db, s)
        db.close()
