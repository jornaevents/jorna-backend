import pytest
from app.db.models import Booking, User, Service, Vendor
from tests.test_api import TestingSessionLocal, client, make_auth_headers
import uuid


@pytest.fixture
def seeded_db():
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    user = User(
        email=f"test_bookings_{uid}@test.com",
        username=f"test_bookings_{uid}",
        password="pw", phone="1", f_name="A", l_name="B",
        age=20, location="123", gender="M", language="EN", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    vendor_user = User(
        email=f"test_vendor_{uid}@test.com",
        username=f"test_vendor_{uid}",
        password="pw", phone="1", f_name="V", l_name="U",
        age=30, location="456", gender="M", language="EN", token_version=0,
    )
    db.add(vendor_user)
    db.commit()
    db.refresh(vendor_user)

    vendor = Vendor(user_id=vendor_user.user_id, bio="bio", rating=5.0, num_events=1)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    service = Service(name="Test Service", price=100.0, duration_minutes=60, vendor_id=vendor.vendor_id, experience="none")
    db.add(service)
    db.commit()
    db.refresh(service)

    yield {"user": user, "vendor_user": vendor_user, "vendor": vendor, "service": service, "db": db}
    db.close()


def test_create_booking(seeded_db):
    user = seeded_db["user"]
    service = seeded_db["service"]
    headers = make_auth_headers(user)

    response = client.post(
        "/bookings",
        json={
            "service_id": service.service_id,
            "event_name": "Test Event",
            "time_start": "13:00",
            "time_end": "15:00",
            "location": "123 Test St",
            "date_iso": "2026-05-01",
            "venue_latitude": 34.0,
            "venue_longitude": -118.0,
        },
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "pending"
    assert "booking_id" in data


def test_duplicate_booking_returns_existing(seeded_db):
    """Retrying the same slot (same bundle+vendor+service+date) must not
    create a second booking — the existing one is returned."""
    user = seeded_db["user"]
    service = seeded_db["service"]
    db = seeded_db["db"]
    headers = make_auth_headers(user)

    payload = {
        "service_id": service.service_id,
        "event_name": "Dupe Guard Event",
        "time_start": "13:00",
        "time_end": "15:00",
        "location": "123 Test St",
        "date_iso": "2026-06-01",
    }
    first = client.post("/bookings", json=payload, headers=headers)
    assert first.status_code == 200
    first_data = first.json()

    # Retry into the same auto-created bundle → same booking back, no dupe.
    payload["bundle_id"] = first_data["bundle_id"]
    second = client.post("/bookings", json=payload, headers=headers)
    assert second.status_code == 200
    second_data = second.json()
    assert second_data["booking_id"] == first_data["booking_id"]

    count = db.query(Booking).filter(
        Booking.bundle_id == first_data["bundle_id"],
        Booking.service_id == service.service_id,
        Booking.date_iso == "2026-06-01",
    ).count()
    assert count == 1


def test_rebook_allowed_after_rejection(seeded_db):
    """A rejected booking must not block re-booking the same slot."""
    user = seeded_db["user"]
    service = seeded_db["service"]
    db = seeded_db["db"]
    headers = make_auth_headers(user)

    payload = {
        "service_id": service.service_id,
        "event_name": "Rebook Event",
        "time_start": "10:00",
        "time_end": "12:00",
        "location": "123 Test St",
        "date_iso": "2026-06-02",
    }
    first = client.post("/bookings", json=payload, headers=headers)
    assert first.status_code == 200
    first_data = first.json()

    # Vendor rejects it.
    booking = db.query(Booking).filter(Booking.booking_id == first_data["booking_id"]).first()
    booking.status = "rejected"
    db.commit()

    # Same slot books again — new booking, not the rejected one.
    payload["bundle_id"] = first_data["bundle_id"]
    second = client.post("/bookings", json=payload, headers=headers)
    assert second.status_code == 200
    assert second.json()["booking_id"] != first_data["booking_id"]


def test_approve_booking(seeded_db):
    db = seeded_db["db"]
    user = seeded_db["user"]
    vendor = seeded_db["vendor"]
    vendor_user = seeded_db["vendor_user"]
    service = seeded_db["service"]

    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="10:00", time_end="11:30",
        location="145 Main St", date_iso="2026-03-02",
        venue_latitude=40.0, venue_longitude=-70.0, status="pending",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    # Vendor approves
    headers = make_auth_headers(vendor_user)
    response = client.put(
        f"/bookings/{booking.booking_id}/status",
        json={"status": "approved"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["message"] == "Booking successfully updated to approved"

    db.refresh(booking)
    assert booking.status == "approved"


def test_client_cannot_approve(seeded_db):
    db = seeded_db["db"]
    user = seeded_db["user"]
    vendor = seeded_db["vendor"]
    service = seeded_db["service"]

    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="10:00", time_end="11:30",
        location="145 Main St", date_iso="2026-03-02",
        venue_latitude=40.0, venue_longitude=-70.0, status="pending",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    # Client tries to approve — should be rejected (only vendors can approve)
    headers = make_auth_headers(user)
    response = client.put(
        f"/bookings/{booking.booking_id}/status",
        json={"status": "approved"},
        headers=headers,
    )
    assert response.status_code == 403


def test_get_user_bookings(seeded_db):
    db = seeded_db["db"]
    user = seeded_db["user"]
    vendor = seeded_db["vendor"]
    service = seeded_db["service"]

    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="10:00", time_end="11:30",
        location="145 Main St", date_iso="2026-03-02", status="pending",
    )
    db.add(booking)
    db.commit()

    headers = make_auth_headers(user)
    response = client.get(f"/bookings/user/{user.user_id}", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert len(data["items"]) > 0
    assert "total" in data


def test_get_user_bookings_forbidden(seeded_db):
    """A user cannot fetch another user's bookings."""
    user = seeded_db["user"]
    vendor_user = seeded_db["vendor_user"]

    headers = make_auth_headers(vendor_user)
    response = client.get(f"/bookings/user/{user.user_id}", headers=headers)
    assert response.status_code == 403


def test_get_vendor_bookings(seeded_db):
    db = seeded_db["db"]
    user = seeded_db["user"]
    vendor = seeded_db["vendor"]
    vendor_user = seeded_db["vendor_user"]
    service = seeded_db["service"]

    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="10:00", time_end="11:30",
        location="145 Main St", date_iso="2026-03-02", status="pending",
    )
    db.add(booking)
    db.commit()

    headers = make_auth_headers(vendor_user)
    response = client.get(f"/bookings/vendor/{vendor.vendor_id}", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert len(data["items"]) > 0


def test_get_vendor_bookings_forbidden(seeded_db):
    """A non-vendor user cannot fetch another vendor's bookings."""
    vendor = seeded_db["vendor"]
    user = seeded_db["user"]

    headers = make_auth_headers(user)
    response = client.get(f"/bookings/vendor/{vendor.vendor_id}", headers=headers)
    assert response.status_code == 403


def test_unauthenticated_booking_rejected():
    """Requests without a token should be rejected with 401 or 403."""
    response = client.post(
        "/bookings",
        json={
            "service_id": "any-id",
            "event_name": "Test",
            "time_start": "10:00",
            "time_end": "11:00",
            "location": "Somewhere",
            "date_iso": "2026-05-01",
        },
    )
    assert response.status_code in (401, 403)
