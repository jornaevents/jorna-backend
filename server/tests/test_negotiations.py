"""Tests for the price negotiation system."""

import uuid
import pytest
from datetime import datetime, timezone
from app.db.models import Booking, User, Vendor, Service
from tests.test_api import TestingSessionLocal, client, make_auth_headers


@pytest.fixture
def seeded_db():
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    client_user = User(
        email=f"neg_client_{uid}@test.com", username=f"neg_client_{uid}",
        password="pw", phone="1", f_name="Client", l_name="User",
        age=25, location="123", gender="M", language="EN", token_version=0,
    )
    vendor_user = User(
        email=f"neg_vendor_{uid}@test.com", username=f"neg_vendor_{uid}",
        password="pw", phone="1", f_name="Vendor", l_name="User",
        age=30, location="456", gender="F", language="EN", token_version=0,
    )
    db.add_all([client_user, vendor_user])
    db.commit()
    db.refresh(client_user)
    db.refresh(vendor_user)

    vendor = Vendor(user_id=vendor_user.user_id, bio="bio", rating=4.5, num_events=10)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    # Negotiation is a per-service opt-in (default off); these tests exercise the
    # negotiation flow, so the seeded service must be negotiable.
    service = Service(name="DJ Set", price=1000.0, duration_minutes=240,
                      vendor_id=vendor.vendor_id, experience="5 years",
                      negotiable=True)
    db.add(service)
    db.commit()
    db.refresh(service)

    booking = Booking(
        user_id=client_user.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id,        time_start="18:00", time_end="23:00", location="Hall",
        date_iso="2026-10-01", status="approved", payment_status="unpaid",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    yield {
        "client_user": client_user, "vendor_user": vendor_user,
        "vendor": vendor, "service": service, "booking": booking, "db": db,
    }
    db.close()


# ── Start negotiation ─────────────────────────────────────────────────

def test_client_can_start_negotiation(seeded_db):
    client_user = seeded_db["client_user"]
    booking = seeded_db["booking"]
    headers = make_auth_headers(client_user)

    response = client.post("/negotiations", json={
        "booking_id": booking.booking_id,
        "amount_cents": 80000,
        "message": "Can you do $800?",
    }, headers=headers)

    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "open"
    assert data["current_offer_cents"] == 80000
    assert data["current_offer_dollars"] == 800.0
    assert data["proposed_by"] == client_user.user_id
    assert len(data["offers"]) == 1
    assert data["offers"][0]["action"] == "offer"


def test_vendor_can_start_negotiation(seeded_db):
    vendor_user = seeded_db["vendor_user"]
    booking = seeded_db["booking"]
    headers = make_auth_headers(vendor_user)

    response = client.post("/negotiations", json={
        "booking_id": booking.booking_id,
        "amount_cents": 120000,
        "message": "Best I can do is $1200.",
    }, headers=headers)

    assert response.status_code == 201
    assert response.json()["proposed_by"] == vendor_user.user_id


def test_third_party_cannot_start_negotiation(seeded_db):
    db = seeded_db["db"]
    uid = str(uuid.uuid4())[:8]
    stranger = User(
        email=f"stranger_{uid}@test.com", username=f"stranger_{uid}",
        password="pw", phone="1", f_name="S", l_name="T",
        age=20, location="789", gender="M", language="EN", token_version=0,
    )
    db.add(stranger)
    db.commit()
    db.refresh(stranger)

    response = client.post("/negotiations", json={
        "booking_id": seeded_db["booking"].booking_id,
        "amount_cents": 50000,
    }, headers=make_auth_headers(stranger))
    assert response.status_code == 403


def test_duplicate_negotiation_rejected(seeded_db):
    client_user = seeded_db["client_user"]
    booking = seeded_db["booking"]
    headers = make_auth_headers(client_user)

    client.post("/negotiations", json={"booking_id": booking.booking_id, "amount_cents": 80000}, headers=headers)
    response = client.post("/negotiations", json={"booking_id": booking.booking_id, "amount_cents": 70000}, headers=headers)
    assert response.status_code == 400


def test_zero_amount_rejected(seeded_db):
    client_user = seeded_db["client_user"]
    booking = seeded_db["booking"]
    response = client.post("/negotiations", json={
        "booking_id": booking.booking_id, "amount_cents": 0,
    }, headers=make_auth_headers(client_user))
    assert response.status_code == 422  # Pydantic gt=0 validation


# ── Get negotiation ───────────────────────────────────────────────────

def test_get_negotiation(seeded_db):
    client_user = seeded_db["client_user"]
    booking = seeded_db["booking"]
    headers = make_auth_headers(client_user)

    client.post("/negotiations", json={"booking_id": booking.booking_id, "amount_cents": 80000}, headers=headers)

    response = client.get(f"/negotiations/booking/{booking.booking_id}", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["current_offer_cents"] == 80000
    assert len(data["offers"]) == 1


def test_get_negotiation_not_found(seeded_db):
    client_user = seeded_db["client_user"]
    booking = seeded_db["booking"]
    response = client.get(f"/negotiations/booking/{booking.booking_id}",
                          headers=make_auth_headers(client_user))
    assert response.status_code == 404


# ── Counter offer ─────────────────────────────────────────────────────

def test_other_party_can_counter(seeded_db):
    client_user = seeded_db["client_user"]
    vendor_user = seeded_db["vendor_user"]
    booking = seeded_db["booking"]

    start_resp = client.post("/negotiations", json={
        "booking_id": booking.booking_id, "amount_cents": 80000,
    }, headers=make_auth_headers(client_user))
    neg_id = start_resp.json()["negotiation_id"]

    response = client.post(f"/negotiations/{neg_id}/offer", json={
        "amount_cents": 90000, "message": "How about $900?",
    }, headers=make_auth_headers(vendor_user))

    assert response.status_code == 200
    data = response.json()
    assert data["current_offer_cents"] == 90000
    assert data["proposed_by"] == vendor_user.user_id
    assert len(data["offers"]) == 2
    assert data["offers"][1]["action"] == "counter"


def test_same_party_cannot_counter_own_offer(seeded_db):
    client_user = seeded_db["client_user"]
    booking = seeded_db["booking"]
    headers = make_auth_headers(client_user)

    start_resp = client.post("/negotiations", json={
        "booking_id": booking.booking_id, "amount_cents": 80000,
    }, headers=headers)
    neg_id = start_resp.json()["negotiation_id"]

    response = client.post(f"/negotiations/{neg_id}/offer", json={"amount_cents": 75000}, headers=headers)
    assert response.status_code == 400


# ── Accept ────────────────────────────────────────────────────────────

def test_vendor_can_accept_client_offer(seeded_db):
    client_user = seeded_db["client_user"]
    vendor_user = seeded_db["vendor_user"]
    booking = seeded_db["booking"]
    db = seeded_db["db"]

    start_resp = client.post("/negotiations", json={
        "booking_id": booking.booking_id, "amount_cents": 85000,
    }, headers=make_auth_headers(client_user))
    neg_id = start_resp.json()["negotiation_id"]

    response = client.post(f"/negotiations/{neg_id}/accept",
                           headers=make_auth_headers(vendor_user))
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    assert data["offers"][-1]["action"] == "accept"

    # Booking amount_cents should be updated
    db.refresh(booking)
    assert booking.amount_cents == 85000


def test_cannot_accept_own_offer(seeded_db):
    client_user = seeded_db["client_user"]
    booking = seeded_db["booking"]
    headers = make_auth_headers(client_user)

    start_resp = client.post("/negotiations", json={
        "booking_id": booking.booking_id, "amount_cents": 85000,
    }, headers=headers)
    neg_id = start_resp.json()["negotiation_id"]

    response = client.post(f"/negotiations/{neg_id}/accept", headers=headers)
    assert response.status_code == 400


def test_cannot_act_on_closed_negotiation(seeded_db):
    client_user = seeded_db["client_user"]
    vendor_user = seeded_db["vendor_user"]
    booking = seeded_db["booking"]

    start_resp = client.post("/negotiations", json={
        "booking_id": booking.booking_id, "amount_cents": 85000,
    }, headers=make_auth_headers(client_user))
    neg_id = start_resp.json()["negotiation_id"]

    client.post(f"/negotiations/{neg_id}/accept", headers=make_auth_headers(vendor_user))

    # Try to counter after accepted — should fail
    response = client.post(f"/negotiations/{neg_id}/offer", json={"amount_cents": 70000},
                           headers=make_auth_headers(vendor_user))
    assert response.status_code == 400


# ── Reject ────────────────────────────────────────────────────────────

def test_vendor_can_reject(seeded_db):
    client_user = seeded_db["client_user"]
    vendor_user = seeded_db["vendor_user"]
    booking = seeded_db["booking"]
    db = seeded_db["db"]

    start_resp = client.post("/negotiations", json={
        "booking_id": booking.booking_id, "amount_cents": 50000,
    }, headers=make_auth_headers(client_user))
    neg_id = start_resp.json()["negotiation_id"]

    response = client.post(f"/negotiations/{neg_id}/reject",
                           json={"message": "Can't go that low, sorry."},
                           headers=make_auth_headers(vendor_user))
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "rejected"
    assert data["offers"][-1]["action"] == "reject"

    # Booking price should NOT be changed
    db.refresh(booking)
    assert booking.amount_cents is None


# ── Full negotiation flow ─────────────────────────────────────────────

def test_full_negotiation_flow(seeded_db):
    """Client offers → vendor counters → client accepts."""
    client_user = seeded_db["client_user"]
    vendor_user = seeded_db["vendor_user"]
    booking = seeded_db["booking"]
    db = seeded_db["db"]

    # Client opens with $800
    start_resp = client.post("/negotiations", json={
        "booking_id": booking.booking_id, "amount_cents": 80000, "message": "Can you do $800?",
    }, headers=make_auth_headers(client_user))
    assert start_resp.status_code == 201
    neg_id = start_resp.json()["negotiation_id"]

    # Vendor counters at $900
    counter_resp = client.post(f"/negotiations/{neg_id}/offer", json={
        "amount_cents": 90000, "message": "Best I can do is $900.",
    }, headers=make_auth_headers(vendor_user))
    assert counter_resp.status_code == 200
    assert counter_resp.json()["current_offer_cents"] == 90000

    # Client accepts $900
    accept_resp = client.post(f"/negotiations/{neg_id}/accept",
                              headers=make_auth_headers(client_user))
    assert accept_resp.status_code == 200
    assert accept_resp.json()["status"] == "accepted"

    # Booking locked at $900
    db.refresh(booking)
    assert booking.amount_cents == 90000

    # Full history has 3 entries
    history = client.get(f"/negotiations/booking/{booking.booking_id}",
                         headers=make_auth_headers(client_user))
    offers = history.json()["offers"]
    assert len(offers) == 3
    assert [o["action"] for o in offers] == ["offer", "counter", "accept"]


# ── Approving while a negotiation is open (regression) ───────────────

def test_vendor_cannot_approve_while_negotiation_open(seeded_db):
    client_user = seeded_db["client_user"]
    vendor_user = seeded_db["vendor_user"]
    booking = seeded_db["booking"]
    db = seeded_db["db"]

    start = client.post("/negotiations", json={
        "booking_id": booking.booking_id, "amount_cents": 80000,
        "message": "Can you do $800?",
    }, headers=make_auth_headers(client_user))
    assert start.status_code == 201

    response = client.put(
        f"/bookings/{booking.booking_id}/status",
        json={"status": "approved"},
        headers=make_auth_headers(vendor_user),
    )

    assert response.status_code == 409
    assert "open price offer" in response.json()["detail"].lower()

    db.refresh(booking)
    assert booking.status == "negotiation_ongoing"


def test_vendor_can_approve_after_negotiation_resolved(seeded_db):
    """Once the open offer is resolved (here: rejected, price stays as
    listed), the plain Accept path works again exactly as before — proves
    the guard is scoped to an *open* negotiation, not to a booking that
    merely once had one."""
    client_user = seeded_db["client_user"]
    vendor_user = seeded_db["vendor_user"]
    booking = seeded_db["booking"]
    db = seeded_db["db"]

    start = client.post("/negotiations", json={
        "booking_id": booking.booking_id, "amount_cents": 80000,
    }, headers=make_auth_headers(client_user))
    neg_id = start.json()["negotiation_id"]

    reject = client.post(
        f"/negotiations/{neg_id}/reject", json={},
        headers=make_auth_headers(vendor_user),
    )
    assert reject.status_code == 200

    db.refresh(booking)
    assert booking.status == "pending"

    response = client.put(
        f"/bookings/{booking.booking_id}/status",
        json={"status": "approved"},
        headers=make_auth_headers(vendor_user),
    )
    assert response.status_code == 200

    db.refresh(booking)
    assert booking.status == "approved"
