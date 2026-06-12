"""Tests for the event bundle system."""

import uuid
from datetime import datetime, timezone
import pytest
from app.db.models import (
    Booking, Bundle, User, Vendor, Service, Event,
    Negotiation, NegotiationOffer, Message, Review,
    Conversation, GroupMessage, GroupMessageRead,
)
from tests.test_api import TestingSessionLocal, client, make_auth_headers


@pytest.fixture
def seeded_db():
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    user = User(
        email=f"bundle_user_{uid}@test.com", username=f"bundle_user_{uid}",
        password="pw", phone="1", f_name="Bundle", l_name="User",
        age=25, location="123", gender="M", language="EN", token_version=0,
    )
    vendor_user = User(
        email=f"bundle_vendor_{uid}@test.com", username=f"bundle_vendor_{uid}",
        password="pw", phone="1", f_name="Vendor", l_name="User",
        age=30, location="456", gender="F", language="EN", token_version=0,
    )
    db.add_all([user, vendor_user])
    db.commit()
    db.refresh(user)
    db.refresh(vendor_user)

    vendor = Vendor(user_id=vendor_user.user_id, bio="bio", rating=4.5, num_events=10)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    service = Service(name="DJ Set", price=1000.0, duration_minutes=240,
                      vendor_id=vendor.vendor_id, experience="5 years")
    db.add(service)
    db.commit()
    db.refresh(service)

    booking1 = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id,
        time_start="18:00", time_end="23:00", location="Hall",
        date_iso="2026-10-01", status="approved",
    )
    booking2 = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id,
        time_start="12:00", time_end="16:00", location="Hall",
        date_iso="2026-10-01", status="pending",
    )
    db.add_all([booking1, booking2])
    db.commit()
    db.refresh(booking1)
    db.refresh(booking2)

    event = Event(
        user_id=user.user_id, name="Raj's Wedding",
        date_iso="2026-10-01", location="Hall",
        event_type="wedding", guest_count=200,
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    yield {
        "user": user, "vendor_user": vendor_user,
        "vendor": vendor, "service": service,
        "booking1": booking1, "booking2": booking2,
        "event": event, "db": db,
    }
    db.close()


def test_create_empty_bundle(seeded_db):
    user = seeded_db["user"]
    response = client.post("/bundles", json={"name": "My Wedding Bundle"},
                           headers=make_auth_headers(user))
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "My Wedding Bundle"
    assert data["status"] == "draft"
    assert data["booking_count"] == 0
    assert data["total_estimated_cost"] == 0.0


def test_create_bundle_with_bookings(seeded_db):
    user = seeded_db["user"]
    booking1 = seeded_db["booking1"]
    booking2 = seeded_db["booking2"]

    response = client.post("/bundles", json={
        "name": "Full Wedding",
        "booking_ids": [booking1.booking_id, booking2.booking_id],
    }, headers=make_auth_headers(user))

    assert response.status_code == 201
    data = response.json()
    assert data["booking_count"] == 2
    assert data["total_estimated_cost"] == 2000.0


def test_create_bundle_with_event(seeded_db):
    user = seeded_db["user"]
    event = seeded_db["event"]

    response = client.post("/bundles", json={
        "name": "Wedding Vendors",
        "event_id": event.event_id,
    }, headers=make_auth_headers(user))

    assert response.status_code == 201
    data = response.json()
    assert data["event_id"] == event.event_id
    assert data["event"]["name"] == "Raj's Wedding"


def test_list_bundles(seeded_db):
    user = seeded_db["user"]
    headers = make_auth_headers(user)

    client.post("/bundles", json={"name": "Bundle A"}, headers=headers)
    client.post("/bundles", json={"name": "Bundle B"}, headers=headers)

    response = client.get("/bundles", headers=headers)
    assert response.status_code == 200
    names = [b["name"] for b in response.json()]
    assert "Bundle A" in names
    assert "Bundle B" in names


def test_get_bundle(seeded_db):
    user = seeded_db["user"]
    booking1 = seeded_db["booking1"]
    headers = make_auth_headers(user)

    create_resp = client.post("/bundles", json={
        "name": "Test Bundle",
        "booking_ids": [booking1.booking_id],
    }, headers=headers)
    bundle_id = create_resp.json()["bundle_id"]

    response = client.get(f"/bundles/{bundle_id}", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["booking_count"] == 1
    assert "approved" in data["status_breakdown"]


def test_add_booking_to_bundle(seeded_db):
    user = seeded_db["user"]
    booking1 = seeded_db["booking1"]
    booking2 = seeded_db["booking2"]
    headers = make_auth_headers(user)

    create_resp = client.post("/bundles", json={
        "name": "Test", "booking_ids": [booking1.booking_id],
    }, headers=headers)
    bundle_id = create_resp.json()["bundle_id"]

    response = client.post(f"/bundles/{bundle_id}/bookings/{booking2.booking_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["booking_count"] == 2


def test_remove_booking_from_bundle(seeded_db):
    user = seeded_db["user"]
    booking1 = seeded_db["booking1"]
    booking2 = seeded_db["booking2"]
    headers = make_auth_headers(user)

    create_resp = client.post("/bundles", json={
        "name": "Test",
        "booking_ids": [booking1.booking_id, booking2.booking_id],
    }, headers=headers)
    bundle_id = create_resp.json()["bundle_id"]

    response = client.delete(f"/bundles/{bundle_id}/bookings/{booking1.booking_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["booking_count"] == 1


def test_booking_cannot_be_in_two_bundles(seeded_db):
    user = seeded_db["user"]
    booking1 = seeded_db["booking1"]
    headers = make_auth_headers(user)

    client.post("/bundles", json={"name": "Bundle A", "booking_ids": [booking1.booking_id]}, headers=headers)
    resp_b = client.post("/bundles", json={"name": "Bundle B"}, headers=headers)
    bundle_b_id = resp_b.json()["bundle_id"]

    response = client.post(f"/bundles/{bundle_b_id}/bookings/{booking1.booking_id}", headers=headers)
    assert response.status_code == 400


def test_update_bundle_status(seeded_db):
    user = seeded_db["user"]
    headers = make_auth_headers(user)

    create_resp = client.post("/bundles", json={"name": "Status Test"}, headers=headers)
    bundle_id = create_resp.json()["bundle_id"]

    response = client.patch(f"/bundles/{bundle_id}/status", json={"status": "confirmed"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"


def test_invalid_status_rejected(seeded_db):
    user = seeded_db["user"]
    headers = make_auth_headers(user)

    create_resp = client.post("/bundles", json={"name": "Status Test"}, headers=headers)
    bundle_id = create_resp.json()["bundle_id"]

    response = client.patch(f"/bundles/{bundle_id}/status", json={"status": "invalid"}, headers=headers)
    assert response.status_code == 400


def test_delete_bundle_deletes_bookings(seeded_db):
    user = seeded_db["user"]
    booking1 = seeded_db["booking1"]
    db = seeded_db["db"]
    headers = make_auth_headers(user)

    create_resp = client.post("/bundles", json={
        "name": "To Delete", "booking_ids": [booking1.booking_id],
    }, headers=headers)
    bundle_id = create_resp.json()["bundle_id"]

    response = client.delete(f"/bundles/{bundle_id}", headers=headers)
    assert response.status_code == 204

    assert db.query(Booking).filter(Booking.booking_id == booking1.booking_id).first() is None
    assert db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first() is None


def test_delete_bundle_with_negotiations_and_chats(seeded_db):
    """Deleting a confirmed bundle should also clean up negotiations, messages,
    reviews, and group conversations tied to its bookings."""
    user = seeded_db["user"]
    vendor_user = seeded_db["vendor_user"]
    vendor = seeded_db["vendor"]
    booking1 = seeded_db["booking1"]
    booking1_id = booking1.booking_id
    db = seeded_db["db"]
    headers = make_auth_headers(user)

    create_resp = client.post("/bundles", json={
        "name": "Full Bundle", "booking_ids": [booking1_id],
    }, headers=headers)
    bundle_id = create_resp.json()["bundle_id"]

    confirm_resp = client.patch(f"/bundles/{bundle_id}/status", json={"status": "confirmed"}, headers=headers)
    assert confirm_resp.status_code == 200

    now = datetime.now(timezone.utc)

    negotiation = Negotiation(
        booking_id=booking1_id, status="open",
        current_offer_cents=10000, proposed_by=user.user_id,
        created_at=now, updated_at=now,
    )
    db.add(negotiation)
    db.commit()
    negotiation_id = negotiation.negotiation_id

    db.add(NegotiationOffer(
        negotiation_id=negotiation_id, proposed_by=user.user_id,
        action="offer", amount_cents=10000, message="test offer", created_at=now,
    ))
    db.add(Message(
        booking_id=booking1_id, sender_id=user.user_id, receiver_id=vendor_user.user_id,
        content="hello", created_at=now, is_read=False,
    ))
    db.add(Review(
        booking_id=booking1_id, vendor_id=vendor.vendor_id, user_id=user.user_id,
        rating=5.0, comment="great", created_at=now,
    ))
    db.commit()

    conversations = db.query(Conversation).filter(Conversation.bundle_id == bundle_id).all()
    assert len(conversations) > 0
    conv_id = conversations[0].conversation_id

    group_msg = GroupMessage(
        conversation_id=conv_id, sender_id=user.user_id,
        content="hi everyone", created_at=now,
    )
    db.add(group_msg)
    db.commit()
    group_msg_id = group_msg.message_id

    db.add(GroupMessageRead(message_id=group_msg_id, user_id=user.user_id, read_at=now))
    db.commit()

    response = client.delete(f"/bundles/{bundle_id}", headers=headers)
    assert response.status_code == 204

    assert db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first() is None
    assert db.query(Booking).filter(Booking.booking_id == booking1_id).first() is None
    assert db.query(Negotiation).filter(Negotiation.booking_id == booking1_id).first() is None
    assert db.query(Conversation).filter(Conversation.bundle_id == bundle_id).first() is None
    assert db.query(GroupMessage).filter(GroupMessage.conversation_id == conv_id).first() is None


def test_other_user_cannot_access_bundle(seeded_db):
    user = seeded_db["user"]
    vendor_user = seeded_db["vendor_user"]

    create_resp = client.post("/bundles", json={"name": "Private"},
                              headers=make_auth_headers(user))
    bundle_id = create_resp.json()["bundle_id"]

    response = client.get(f"/bundles/{bundle_id}", headers=make_auth_headers(vendor_user))
    assert response.status_code == 403
