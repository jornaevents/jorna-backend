"""Tests for bundle group conversations."""

import uuid
import pytest
from app.db.models import Booking, User, Vendor, Service
from tests.test_api import TestingSessionLocal, client, make_auth_headers


@pytest.fixture
def seeded_db():
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    client_user = User(
        email=f"conv_client_{uid}@test.com", username=f"conv_client_{uid}",
        password="pw", phone="1", f_name="Client", l_name="User",
        age=25, location="123", gender="M", language="EN", token_version=0,
    )
    vendor_user1 = User(
        email=f"conv_vendor1_{uid}@test.com", username=f"conv_vendor1_{uid}",
        password="pw", phone="1", f_name="Vendor", l_name="One",
        age=30, location="456", gender="F", language="EN", token_version=0,
    )
    vendor_user2 = User(
        email=f"conv_vendor2_{uid}@test.com", username=f"conv_vendor2_{uid}",
        password="pw", phone="1", f_name="Vendor", l_name="Two",
        age=32, location="789", gender="M", language="EN", token_version=0,
    )
    db.add_all([client_user, vendor_user1, vendor_user2])
    db.commit()
    for u in [client_user, vendor_user1, vendor_user2]:
        db.refresh(u)

    vendor1 = Vendor(user_id=vendor_user1.user_id, bio="DJ", rating=4.8, num_events=20)
    vendor2 = Vendor(user_id=vendor_user2.user_id, bio="Catering", rating=4.9, num_events=30)
    db.add_all([vendor1, vendor2])
    db.commit()
    db.refresh(vendor1)
    db.refresh(vendor2)

    service1 = Service(name="DJ Set", price=1000.0, duration_minutes=240,
                       vendor_id=vendor1.vendor_id, experience="5 years")
    service2 = Service(name="Catering", price=3000.0, duration_minutes=300,
                       vendor_id=vendor2.vendor_id, experience="10 years")
    db.add_all([service1, service2])
    db.commit()
    db.refresh(service1)
    db.refresh(service2)

    booking1 = Booking(
        user_id=client_user.user_id, vendor_id=vendor1.vendor_id,
        service_id=service1.service_id,        time_start="18:00", time_end="23:00", location="Hall",
        date_iso="2026-10-01", status="approved",
    )
    booking2 = Booking(
        user_id=client_user.user_id, vendor_id=vendor2.vendor_id,
        service_id=service2.service_id,        time_start="12:00", time_end="16:00", location="Hall",
        date_iso="2026-10-01", status="approved",
    )
    db.add_all([booking1, booking2])
    db.commit()
    db.refresh(booking1)
    db.refresh(booking2)

    yield {
        "client_user": client_user,
        "vendor_user1": vendor_user1, "vendor_user2": vendor_user2,
        "vendor1": vendor1, "vendor2": vendor2,
        "booking1": booking1, "booking2": booking2,
        "db": db,
    }
    db.close()


def _create_bundle(seeded_db, confirm=True) -> tuple[str, list[str]]:
    """Helper: create a bundle and return (bundle_id, [conversation_ids])."""
    client_user = seeded_db["client_user"]
    booking1 = seeded_db["booking1"]
    booking2 = seeded_db["booking2"]
    resp = client.post("/bundles", json={
        "name": "Wedding Bundle",
        "booking_ids": [booking1.booking_id, booking2.booking_id],
    }, headers=make_auth_headers(client_user))
    assert resp.status_code == 201
    bundle_id = resp.json()["bundle_id"]

    if confirm:
        confirm_resp = client.patch(f"/bundles/{bundle_id}/status",
                                    json={"status": "confirmed"},
                                    headers=make_auth_headers(client_user))
        assert confirm_resp.status_code == 200

    convs = client.get("/conversations", headers=make_auth_headers(client_user))
    conv_ids = [c["conversation_id"] for c in convs.json()]
    return bundle_id, conv_ids


# ── Auto-creation ─────────────────────────────────────────────────────

def test_bundle_creation_creates_two_conversations(seeded_db):
    """Client sees 1 (all_parties), vendors see 2 (vendors_only + all_parties)."""
    vendor_user1 = seeded_db["vendor_user1"]
    _create_bundle(seeded_db)

    vendor_convs = client.get("/conversations", headers=make_auth_headers(vendor_user1)).json()
    assert len(vendor_convs) == 2


def test_conversations_have_correct_types(seeded_db):
    vendor_user1 = seeded_db["vendor_user1"]
    _create_bundle(seeded_db)

    convs = client.get("/conversations", headers=make_auth_headers(vendor_user1)).json()
    types = {c["type"] for c in convs}
    assert "vendors_only" in types
    assert "all_parties" in types


def test_client_sees_only_all_parties(seeded_db):
    """Client is not a member of vendors_only so only sees all_parties."""
    client_user = seeded_db["client_user"]
    _create_bundle(seeded_db)

    convs = client.get("/conversations", headers=make_auth_headers(client_user)).json()
    assert len(convs) == 1
    assert convs[0]["type"] == "all_parties"


def test_vendors_only_excludes_client(seeded_db):
    vendor_user1 = seeded_db["vendor_user1"]
    client_user = seeded_db["client_user"]
    _create_bundle(seeded_db)

    convs = client.get("/conversations", headers=make_auth_headers(vendor_user1)).json()
    vendors_only = next(c for c in convs if c["type"] == "vendors_only")
    member_ids = [m["user_id"] for m in vendors_only["members"]]
    assert client_user.user_id not in member_ids


def test_all_parties_includes_client_and_vendors(seeded_db):
    client_user = seeded_db["client_user"]
    vendor_user1 = seeded_db["vendor_user1"]
    vendor_user2 = seeded_db["vendor_user2"]
    _create_bundle(seeded_db)

    convs = client.get("/conversations", headers=make_auth_headers(client_user)).json()
    all_parties = next(c for c in convs if c["type"] == "all_parties")
    member_ids = {m["user_id"] for m in all_parties["members"]}
    assert client_user.user_id in member_ids
    assert vendor_user1.user_id in member_ids
    assert vendor_user2.user_id in member_ids


def test_vendors_can_see_their_conversations(seeded_db):
    vendor_user1 = seeded_db["vendor_user1"]
    _create_bundle(seeded_db)

    convs = client.get("/conversations", headers=make_auth_headers(vendor_user1)).json()
    assert len(convs) == 2


# ── Messaging ─────────────────────────────────────────────────────────

def test_member_can_send_message(seeded_db):
    client_user = seeded_db["client_user"]
    _, conv_ids = _create_bundle(seeded_db)

    convs = client.get("/conversations", headers=make_auth_headers(client_user)).json()
    all_parties_id = next(c["conversation_id"] for c in convs if c["type"] == "all_parties")

    response = client.post(f"/conversations/{all_parties_id}/messages",
                           json={"content": "Hello everyone!"},
                           headers=make_auth_headers(client_user))
    assert response.status_code == 201
    data = response.json()
    assert data["content"] == "Hello everyone!"
    assert data["sender_id"] == client_user.user_id


def test_non_member_cannot_send_message(seeded_db):
    db = seeded_db["db"]
    uid = str(uuid.uuid4())[:8]
    stranger = User(
        email=f"stranger_{uid}@test.com", username=f"stranger_{uid}",
        password="pw", phone="1", f_name="S", l_name="T",
        age=20, location="999", gender="M", language="EN", token_version=0,
    )
    db.add(stranger)
    db.commit()
    db.refresh(stranger)

    client_user = seeded_db["client_user"]
    _create_bundle(seeded_db)
    convs = client.get("/conversations", headers=make_auth_headers(client_user)).json()
    conv_id = convs[0]["conversation_id"]

    response = client.post(f"/conversations/{conv_id}/messages",
                           json={"content": "Sneaky"},
                           headers=make_auth_headers(stranger))
    assert response.status_code == 403


def test_get_messages_marks_as_read(seeded_db):
    client_user = seeded_db["client_user"]
    vendor_user1 = seeded_db["vendor_user1"]
    _create_bundle(seeded_db)

    convs = client.get("/conversations", headers=make_auth_headers(client_user)).json()
    all_parties_id = next(c["conversation_id"] for c in convs if c["type"] == "all_parties")

    # Client sends a message
    client.post(f"/conversations/{all_parties_id}/messages",
                json={"content": "Hi vendors!"},
                headers=make_auth_headers(client_user))

    # Vendor fetches — should be marked as read
    response = client.get(f"/conversations/{all_parties_id}/messages",
                          headers=make_auth_headers(vendor_user1))
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert client_user.user_id in items[0]["read_by"]


def test_unread_count(seeded_db):
    client_user = seeded_db["client_user"]
    vendor_user1 = seeded_db["vendor_user1"]
    _create_bundle(seeded_db)

    convs = client.get("/conversations", headers=make_auth_headers(client_user)).json()
    all_parties_id = next(c["conversation_id"] for c in convs if c["type"] == "all_parties")

    # Client sends 2 messages
    for msg in ["Message 1", "Message 2"]:
        client.post(f"/conversations/{all_parties_id}/messages",
                    json={"content": msg},
                    headers=make_auth_headers(client_user))

    # Vendor1 hasn't read them yet
    response = client.get("/conversations/unread-count", headers=make_auth_headers(vendor_user1))
    assert response.status_code == 200
    assert response.json()["unread_count"] >= 2


def test_empty_message_rejected(seeded_db):
    client_user = seeded_db["client_user"]
    _create_bundle(seeded_db)
    convs = client.get("/conversations", headers=make_auth_headers(client_user)).json()
    conv_id = convs[0]["conversation_id"]

    response = client.post(f"/conversations/{conv_id}/messages",
                           json={"content": "   "},
                           headers=make_auth_headers(client_user))
    assert response.status_code == 400
