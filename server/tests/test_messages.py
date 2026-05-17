"""Tests for booking-scoped messaging."""

import uuid
import pytest
from app.db.models import Booking, User, Vendor, Service
from tests.test_api import TestingSessionLocal, client, make_auth_headers


@pytest.fixture
def seeded_db():
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    client_user = User(
        email=f"msg_client_{uid}@test.com", username=f"msg_client_{uid}",
        password="pw", phone="1", f_name="Client", l_name="User",
        age=25, location="123", gender="M", language="EN", token_version=0,
    )
    vendor_user = User(
        email=f"msg_vendor_{uid}@test.com", username=f"msg_vendor_{uid}",
        password="pw", phone="1", f_name="Vendor", l_name="User",
        age=30, location="456", gender="F", language="EN", token_version=0,
    )
    db.add_all([client_user, vendor_user])
    db.commit()
    db.refresh(client_user)
    db.refresh(vendor_user)

    vendor = Vendor(user_id=vendor_user.user_id, bio="bio", rating=0.0, num_events=0)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    service = Service(name="DJ", price=300.0, duration_minutes=180,
                      vendor_id=vendor.vendor_id, experience="3 years")
    db.add(service)
    db.commit()
    db.refresh(service)

    booking = Booking(
        user_id=client_user.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id, event_name="Party",
        time_start="18:00", time_end="22:00", location="Venue",
        date_iso="2026-08-01", status="approved",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    yield {
        "client_user": client_user, "vendor_user": vendor_user,
        "vendor": vendor, "booking": booking, "db": db,
    }
    db.close()


def test_client_can_send_message(seeded_db):
    client_user = seeded_db["client_user"]
    booking = seeded_db["booking"]
    headers = make_auth_headers(client_user)

    response = client.post("/messages", json={
        "booking_id": booking.booking_id,
        "content": "Hello, is 6pm still fine?",
    }, headers=headers)

    assert response.status_code == 201
    data = response.json()
    assert data["sender_id"] == client_user.user_id
    assert data["content"] == "Hello, is 6pm still fine?"
    assert data["is_read"] is False


def test_vendor_can_send_message(seeded_db):
    vendor_user = seeded_db["vendor_user"]
    booking = seeded_db["booking"]
    headers = make_auth_headers(vendor_user)

    response = client.post("/messages", json={
        "booking_id": booking.booking_id,
        "content": "Yes, 6pm works perfectly!",
    }, headers=headers)

    assert response.status_code == 201
    assert response.json()["sender_id"] == vendor_user.user_id


def test_third_party_cannot_send_message(seeded_db):
    uid = str(uuid.uuid4())[:8]
    db = seeded_db["db"]
    stranger = User(
        email=f"stranger_{uid}@test.com", username=f"stranger_{uid}",
        password="pw", phone="1", f_name="S", l_name="T",
        age=20, location="789", gender="M", language="EN", token_version=0,
    )
    db.add(stranger)
    db.commit()
    db.refresh(stranger)

    headers = make_auth_headers(stranger)
    response = client.post("/messages", json={
        "booking_id": seeded_db["booking"].booking_id,
        "content": "Sneaky message",
    }, headers=headers)
    assert response.status_code == 403


def test_empty_message_rejected(seeded_db):
    client_user = seeded_db["client_user"]
    booking = seeded_db["booking"]
    headers = make_auth_headers(client_user)

    response = client.post("/messages", json={
        "booking_id": booking.booking_id,
        "content": "   ",
    }, headers=headers)
    assert response.status_code == 400


def test_get_conversation(seeded_db):
    client_user = seeded_db["client_user"]
    vendor_user = seeded_db["vendor_user"]
    booking = seeded_db["booking"]

    client.post("/messages", json={"booking_id": booking.booking_id, "content": "Hi!"},
                headers=make_auth_headers(client_user))
    client.post("/messages", json={"booking_id": booking.booking_id, "content": "Hello!"},
                headers=make_auth_headers(vendor_user))

    response = client.get(f"/messages/booking/{booking.booking_id}",
                          headers=make_auth_headers(client_user))
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "total" in data
    assert len(data["items"]) >= 2


def test_messages_marked_as_read_on_fetch(seeded_db):
    client_user = seeded_db["client_user"]
    vendor_user = seeded_db["vendor_user"]
    booking = seeded_db["booking"]

    # Vendor sends message to client
    client.post("/messages", json={"booking_id": booking.booking_id, "content": "Check this out"},
                headers=make_auth_headers(vendor_user))

    # Client fetches — message should be auto-marked as read
    response = client.get(f"/messages/booking/{booking.booking_id}",
                          headers=make_auth_headers(client_user))
    assert response.status_code == 200
    items = response.json()["items"]
    client_received = [m for m in items if m["receiver_id"] == client_user.user_id]
    assert all(m["is_read"] for m in client_received)


def test_unread_count(seeded_db):
    client_user = seeded_db["client_user"]
    vendor_user = seeded_db["vendor_user"]
    booking = seeded_db["booking"]

    # Vendor sends two messages
    for content in ["Msg 1", "Msg 2"]:
        client.post("/messages", json={"booking_id": booking.booking_id, "content": content},
                    headers=make_auth_headers(vendor_user))

    response = client.get("/messages/unread-count", headers=make_auth_headers(client_user))
    assert response.status_code == 200
    assert response.json()["unread_count"] >= 2


def test_mark_single_message_read(seeded_db):
    client_user = seeded_db["client_user"]
    vendor_user = seeded_db["vendor_user"]
    booking = seeded_db["booking"]

    send_resp = client.post("/messages", json={"booking_id": booking.booking_id, "content": "Read me"},
                            headers=make_auth_headers(vendor_user))
    message_id = send_resp.json()["message_id"]

    response = client.patch(f"/messages/{message_id}/read",
                            headers=make_auth_headers(client_user))
    assert response.status_code == 200
    assert response.json()["is_read"] is True


def test_cannot_mark_others_message_as_read(seeded_db):
    client_user = seeded_db["client_user"]
    vendor_user = seeded_db["vendor_user"]
    booking = seeded_db["booking"]

    send_resp = client.post("/messages", json={"booking_id": booking.booking_id, "content": "Hi"},
                            headers=make_auth_headers(client_user))
    message_id = send_resp.json()["message_id"]

    # Client sent the message, so vendor is receiver — client shouldn't be able to mark it read
    response = client.patch(f"/messages/{message_id}/read",
                            headers=make_auth_headers(client_user))
    assert response.status_code == 403
