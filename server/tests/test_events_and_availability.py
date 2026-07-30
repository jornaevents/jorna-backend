"""Tests for events CRUD and vendor availability CRUD."""

import uuid
import pytest
from app.db.models import User, Vendor
from tests.test_api import TestingSessionLocal, client, make_auth_headers


@pytest.fixture
def user_and_vendor():
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    user = User(
        email=f"ea_user_{uid}@test.com", username=f"ea_user_{uid}",
        password="pw", phone="1", f_name="A", l_name="B",
        age=25, location="123", gender="M", language="EN", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    vendor = Vendor(user_id=user.user_id, bio="bio", rating=0.0, num_events=0)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    yield {"user": user, "vendor": vendor, "db": db}
    db.close()


# ── Events ────────────────────────────────────────────────────────────

def test_create_event(user_and_vendor):
    user = user_and_vendor["user"]
    headers = make_auth_headers(user)

    response = client.post("/events", json={
        "name": "Diwali Party",
        "date_iso": "2026-10-20",
        "location": "Community Hall",
        "event_type": "festival",
        "guest_count": 150,
        "budget": 5000.0,
        "services_needed": ["dj", "catering"],
    }, headers=headers)

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Diwali Party"
    assert data["guest_count"] == 150
    assert data["user_id"] == user.user_id


def test_list_events(user_and_vendor):
    user = user_and_vendor["user"]
    headers = make_auth_headers(user)

    client.post("/events", json={"name": "Event A", "date_iso": "2026-11-01", "location": "Venue"}, headers=headers)
    client.post("/events", json={"name": "Event B", "date_iso": "2026-12-01", "location": "Venue"}, headers=headers)

    response = client.get("/events", headers=headers)
    assert response.status_code == 200
    names = [e["name"] for e in response.json()]
    assert "Event A" in names
    assert "Event B" in names


def test_update_event(user_and_vendor):
    user = user_and_vendor["user"]
    headers = make_auth_headers(user)

    create_resp = client.post("/events", json={
        "name": "Original Name", "date_iso": "2026-09-01", "location": "Old Venue",
    }, headers=headers)
    event_id = create_resp.json()["event_id"]

    response = client.patch(f"/events/{event_id}", json={"name": "Updated Name", "location": "New Venue"}, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Updated Name"
    assert data["location"] == "New Venue"


def test_delete_event(user_and_vendor):
    user = user_and_vendor["user"]
    headers = make_auth_headers(user)

    create_resp = client.post("/events", json={
        "name": "To Delete", "date_iso": "2026-09-15", "location": "Venue",
    }, headers=headers)
    event_id = create_resp.json()["event_id"]

    response = client.delete(f"/events/{event_id}", headers=headers)
    assert response.status_code == 204

    # Should no longer appear in list
    list_resp = client.get("/events", headers=headers)
    ids = [e["event_id"] for e in list_resp.json()]
    assert event_id not in ids


def test_delete_event_unauthorized(user_and_vendor):
    db = user_and_vendor["db"]
    user = user_and_vendor["user"]
    headers = make_auth_headers(user)

    uid = str(uuid.uuid4())[:8]
    other = User(
        email=f"other_{uid}@test.com", username=f"other_{uid}",
        password="pw", phone="1", f_name="O", l_name="T",
        age=20, location="999", gender="M", language="EN", token_version=0,
    )
    db.add(other)
    db.commit()
    db.refresh(other)

    create_resp = client.post("/events", json={
        "name": "Mine", "date_iso": "2026-10-01", "location": "Venue",
    }, headers=headers)
    event_id = create_resp.json()["event_id"]

    other_headers = make_auth_headers(other)
    response = client.delete(f"/events/{event_id}", headers=other_headers)
    assert response.status_code == 403


# ── Vendor availability ───────────────────────────────────────────────

def test_set_and_get_availability(user_and_vendor):
    user = user_and_vendor["user"]
    headers = make_auth_headers(user)

    slots = [
        {"day_of_week": 0, "start_time": "09:00", "end_time": "17:00"},
        {"day_of_week": 5, "start_time": "10:00", "end_time": "14:00"},
    ]
    response = client.put("/vendors/me/availability", json={"slots": slots}, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    days = {s["day_of_week"] for s in data}
    assert days == {0, 5}


def test_replace_availability(user_and_vendor):
    user = user_and_vendor["user"]
    headers = make_auth_headers(user)

    client.put("/vendors/me/availability", json={"slots": [
        {"day_of_week": 1, "start_time": "08:00", "end_time": "16:00"},
        {"day_of_week": 2, "start_time": "08:00", "end_time": "16:00"},
    ]}, headers=headers)

    # Replace with just one slot
    response = client.put("/vendors/me/availability", json={"slots": [
        {"day_of_week": 3, "start_time": "12:00", "end_time": "18:00"},
    ]}, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["day_of_week"] == 3


def test_clear_availability(user_and_vendor):
    user = user_and_vendor["user"]
    headers = make_auth_headers(user)

    client.put("/vendors/me/availability", json={"slots": [
        {"day_of_week": 0, "start_time": "09:00", "end_time": "17:00"},
    ]}, headers=headers)

    response = client.put("/vendors/me/availability", json={"slots": []}, headers=headers)
    assert response.status_code == 200
    assert response.json() == []


def test_invalid_day_of_week_rejected(user_and_vendor):
    user = user_and_vendor["user"]
    headers = make_auth_headers(user)

    response = client.put("/vendors/me/availability", json={"slots": [
        {"day_of_week": 7, "start_time": "09:00", "end_time": "17:00"},
    ]}, headers=headers)
    assert response.status_code == 400


def test_get_my_availability(user_and_vendor):
    user = user_and_vendor["user"]
    headers = make_auth_headers(user)

    client.put("/vendors/me/availability", json={"slots": [
        {"day_of_week": 4, "start_time": "10:00", "end_time": "15:00"},
    ]}, headers=headers)

    response = client.get("/vendors/me/availability", headers=headers)
    assert response.status_code == 200
    assert any(s["day_of_week"] == 4 for s in response.json())


def test_an_event_with_plans_on_it_cannot_be_deleted():
    """DELETE /events was a way round the bundle cascade's own rule.

    _delete_bundle_cascade removes an event only once the last bundle
    referencing it is gone. This endpoint had no such check, so deleting the
    event directly took the celebration out from under any sibling plan.
    """
    import uuid
    from app.db.models import Bundle, Event, User
    from app.services.event_service import EventError, delete_event
    from tests.test_api import TestingSessionLocal

    db = TestingSessionLocal()
    uid = uuid.uuid4().hex[:8]
    user = User(
        email=f"evtdel_{uid}@test.com", username=f"evtdel_{uid}", password="pw",
        phone="1", f_name="Evt", l_name="Del", age=30, location="NJ",
        gender="F", language="EN", token_version=0,
    )
    db.add(user); db.commit(); db.refresh(user)
    event = Event(user_id=user.user_id, name="Shared celebration",
                  date_iso="2027-06-05", location="Newark, NJ")
    db.add(event); db.commit(); db.refresh(event)
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    bundle = Bundle(user_id=user.user_id, name="Plan A", status="draft",
                    event_id=event.event_id, created_at=now, updated_at=now)
    db.add(bundle); db.commit(); db.refresh(bundle)

    try:
        with pytest.raises(EventError) as caught:
            delete_event(user_id=user.user_id, event_id=event.event_id, db=db)
        assert caught.value.status_code == 400
        assert db.query(Event).filter(Event.event_id == event.event_id).first()

        # With the last plan gone, the celebration is deletable again.
        db.delete(bundle); db.commit()
        delete_event(user_id=user.user_id, event_id=event.event_id, db=db)
        assert db.query(Event).filter(Event.event_id == event.event_id).first() is None
    finally:
        db.query(Bundle).filter(Bundle.bundle_id == bundle.bundle_id).delete()
        db.query(Event).filter(Event.event_id == event.event_id).delete()
        db.query(User).filter(User.user_id == user.user_id).delete()
        db.commit()
        db.close()
