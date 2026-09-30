"""Marketplace requests become proposals (docs/DECISIONS.md #17): accepting
a signed-in client's request sends them a contract to sign — on the same
no-login link as a guest — instead of making the booking final on the spot.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Booking, Negotiation, Service, User, Vendor
from app.limiter import limiter
from tests.test_api import TestingSessionLocal, client, make_auth_headers


@pytest.fixture
def world(monkeypatch):
    import app.services.contract_service as cs

    emails = []
    monkeypatch.setattr(cs, "send_email", lambda **kw: emails.append(kw) or {"success": True})
    limiter.enabled = False
    db = TestingSessionLocal()
    uid = uuid.uuid4().hex[:8]

    def user(prefix):
        u = User(
            email=f"{prefix}_{uid}@test.com", username=f"{prefix}_{uid}", password="pw",
            phone="7325550101", f_name="Priya", l_name="Mehta", age=30, location="NJ",
            gender="F", language="EN", token_version=0,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        return u

    client_user, other_client, vendor_user = user("req_c"), user("req_c2"), user("req_v")
    vendor = Vendor(
        user_id=vendor_user.user_id, bio="DJ", category="music", rating=0.0, num_events=0,
        payment_method="manual", venmo_handle="@dj",
        default_deposit_percent=30,
        default_contract_terms={"travel": "30 miles included."},
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    flat = Service(name="Reception set", price=1000.0, price_unit="event", vendor_id=vendor.vendor_id,
                   experience="e", category="music", negotiable=True)
    per_guest = Service(name="Chaat counter", price=45.0, price_unit="person", vendor_id=vendor.vendor_id,
                        experience="e", category="catering", negotiable=False)
    db.add_all([flat, per_guest])
    db.commit()
    db.refresh(flat)
    db.refresh(per_guest)

    def request(*, date_iso="2027-10-01", service=flat, who=client_user, **extra):
        b = Booking(
            user_id=who.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
            date_iso=date_iso, time_start="18:00", time_end="22:00", location="Pines Manor",
            status="pending", payment_status="unpaid", payment_method="manual", **extra,
        )
        db.add(b)
        db.commit()
        db.refresh(b)
        return b

    yield {
        "db": db, "emails": emails, "vendor": vendor, "flat": flat, "per_guest": per_guest,
        "client": client_user, "other": other_client, "request": request,
        "vendor_h": make_auth_headers(vendor_user), "client_h": make_auth_headers(client_user),
    }
    limiter.enabled = True
    db.close()


def _accept(w, booking):
    return client.put(f"/bookings/{booking.booking_id}/status", json={"status": "approved"}, headers=w["vendor_h"])


def test_a_plain_accept_sends_a_proposal_built_from_the_vendors_usual_terms(world):
    w = world
    req = w["request"]()
    assert _accept(w, req).status_code == 200

    w["db"].refresh(req)
    assert req.status == "approved"
    assert req.contract_status == "sent" and req.contract_token
    assert [i["name"] for i in req.line_items] == ["Reception set"]
    assert [(i["label"], i["amount_cents"]) for i in req.payment_schedule] == [
        ("Deposit", 30_000), ("Final balance", 70_000),
    ]
    assert req.terms_clauses[0]["body"] == "30 miles included."
    assert req.guest_email == w["client"].email

    # The client is emailed their link.
    assert w["emails"][0]["to"] == w["client"].email
    assert f"booking-link?t={req.contract_token}" in w["emails"][0]["html"]

    # And the same no-login page opens it.
    page = client.get(f"/guest-bookings/{req.contract_token}").json()
    assert page["contract_status"] == "viewed"
    assert page["guest_name"] == "Priya Mehta"


def test_nothing_is_owed_until_the_client_signs(world):
    w = world
    req = w["request"]()
    _accept(w, req)
    w["db"].refresh(req)

    early = client.post(f"/payments/bookings/{req.booking_id}/mark-paid", headers=w["client_h"])
    assert early.status_code == 400
    assert "Sign the contract first" in early.json()["detail"]

    signed = client.post(f"/guest-bookings/{req.contract_token}/sign", json={"signer_name": "Priya Mehta"})
    assert signed.status_code == 200, signed.text

    # The in-app buttons pay down the schedule.
    deposit = client.post(f"/payments/bookings/{req.booking_id}/mark-deposit-paid", headers=w["client_h"])
    assert deposit.status_code == 200, deposit.text
    full = client.post(f"/payments/bookings/{req.booking_id}/mark-paid", headers=w["client_h"])
    assert full.status_code == 200, full.text
    w["db"].refresh(req)
    assert req.payment_status == "marked_paid"
    assert all(i["marked_paid_at"] for i in req.payment_schedule)


def test_an_unsigned_accepted_request_only_holds_the_date_until_it_lapses(world):
    w = world
    first = w["request"](date_iso="2027-10-02")
    second = w["request"](date_iso="2027-10-02", who=w["other"])
    _accept(w, first)
    assert _accept(w, second).status_code == 409  # held

    w["db"].refresh(first)
    first.hold_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    w["db"].commit()
    assert _accept(w, second).status_code == 200


def test_accepting_with_a_written_proposal(world):
    w = world
    req = w["request"](date_iso="2027-10-03")
    resp = client.post(f"/bookings/{req.booking_id}/propose", json={
        "line_items": [
            {"kind": "package", "service_id": w["flat"].service_id, "unit_price_cents": 90_000},
            {"kind": "custom", "name": "Uplighting", "unit_price_cents": 10_000},
        ],
        "payment_schedule": [{"label": "Payment in full", "amount_cents": 100_000}],
        "terms_clauses": [{"title": "Meals", "body": "Dinner for two."}],
        "email_client": False,
    }, headers=w["vendor_h"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["amount_cents"] == 100_000
    assert body["terms_clauses"][0]["title"] == "Meals"
    assert body["contract_status"] == "sent"
    assert w["emails"] == []

    again = client.post(f"/bookings/{req.booking_id}/propose", json={}, headers=w["vendor_h"])
    assert again.status_code == 400


def test_an_open_price_offer_blocks_both_ways_of_accepting(world):
    w = world
    req = w["request"](date_iso="2027-10-04")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    w["db"].add(Negotiation(
        booking_id=req.booking_id, proposed_by=w["client"].user_id, status="open",
        current_offer_cents=80_000, created_at=now, updated_at=now,
    ))
    w["db"].commit()
    assert _accept(w, req).status_code == 409
    assert client.post(f"/bookings/{req.booking_id}/propose", json={}, headers=w["vendor_h"]).status_code == 409


def test_a_request_without_a_total_needs_the_builder(world):
    w = world
    req = w["request"](date_iso="2027-10-05", service=w["per_guest"])
    plain = _accept(w, req)
    assert plain.status_code == 409
    assert "contract builder" in plain.json()["detail"]
    w["db"].refresh(req)
    assert req.status == "pending" and req.contract_token is None

    written = client.post(f"/bookings/{req.booking_id}/propose", json={
        "line_items": [{"kind": "package", "service_id": w["per_guest"].service_id, "unit_price_cents": 4_500, "quantity": 150}],
    }, headers=w["vendor_h"])
    assert written.status_code == 200, written.text
    assert written.json()["amount_cents"] == 675_000


def test_the_client_can_decline_an_accepted_request(world):
    w = world
    req = w["request"](date_iso="2027-10-06")
    _accept(w, req)
    w["db"].refresh(req)
    resp = client.post(f"/guest-bookings/{req.contract_token}/decline", json={"reason": "Changed plans"})
    assert resp.status_code == 200, resp.text
    w["db"].refresh(req)
    assert req.status == "rejected" and req.rejected_reason == "client_declined"


def test_a_vendor_backing_out_voids_the_unsigned_proposal(world):
    w = world
    req = w["request"](date_iso="2027-10-07")
    _accept(w, req)
    resp = client.put(f"/bookings/{req.booking_id}/status", json={"status": "rejected"}, headers=w["vendor_h"])
    assert resp.status_code == 200, resp.text
    w["db"].refresh(req)
    assert req.contract_status == "voided"
    assert client.post(
        f"/guest-bookings/{req.contract_token}/sign", json={"signer_name": "Priya Mehta"},
    ).status_code == 410



def test_the_clients_plan_shows_each_payment_and_when_its_due(world):
    """The client app's plan lists every payment on the contract with the
    date the reminder emails count from — never before the signing day."""
    from app.services.bundle_service import _booking_summary

    w = world
    soon = (datetime.now(timezone.utc) + timedelta(days=5)).date().isoformat()
    req = w["request"](date_iso=soon)
    _accept(w, req)
    w["db"].refresh(req)

    def summary():
        w["db"].refresh(req)
        return _booking_summary(req, w["flat"], w["vendor"], w["db"].get(User, w["vendor"].user_id))

    before = summary()["payment_schedule"]
    assert [i["label"] for i in before] == ["Deposit", "Final balance"]
    assert all(i["effective_due"] is None for i in before)  # nothing's owed unsigned

    client.post(f"/guest-bookings/{req.contract_token}/sign", json={"signer_name": "Priya Mehta"})
    today = datetime.now(timezone.utc).date().isoformat()
    deposit, balance = summary()["payment_schedule"]
    assert deposit["effective_due"] == today
    # The balance's own date (days before a 5-days-away event) may already
    # have passed; the client is told it's due today, not that it's late.
    assert balance["effective_due"] >= today
    assert balance["marked_paid_at"] is None

    marked = client.post(f"/guest-bookings/{req.contract_token}/payments/{deposit['id']}/mark-paid")
    assert marked.status_code == 200, marked.text
    deposit, balance = summary()["payment_schedule"]
    assert deposit["marked_paid_at"] and deposit["marked_paid_at"].endswith("+00:00")
    assert balance["marked_paid_at"] is None
    # Leave nothing owed: the reminder tests sweep this same database.
    client.post(f"/guest-bookings/{req.contract_token}/payments/{balance['id']}/mark-paid")
