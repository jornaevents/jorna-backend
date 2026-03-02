import pytest
from app.db.models import Booking, User, Service, Vendor
from tests.test_api import TestingSessionLocal, client

@pytest.fixture
def seeded_db():
    db = TestingSessionLocal()
    import uuid
    unique_id = str(uuid.uuid4())[:8]
    # Create test user
    user = User(
        email=f"test_bookings_{unique_id}@test.com", 
        username=f"test_bookings_{unique_id}", 
        password="pw", phone="1", f_name="A", l_name="B", age=20, location="123", gender="M", language="EN"
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    
    # Create test vendor
    vendor = Vendor(user_id=user.user_id, bio="bio", rating=5.0, num_events=1)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    
    # Create test service
    service = Service(name="Test Service", price=100.0, duration_minutes=60, vendor_id=vendor.vendor_id, experience="none")
    db.add(service)
    db.commit()
    db.refresh(service)
    
    yield {"user": user, "vendor": vendor, "service": service, "db": db}
    
    db.close()


def test_create_booking(seeded_db):
    user = seeded_db["user"]
    vendor = seeded_db["vendor"]
    service = seeded_db["service"]
    
    response = client.post(
        "/bookings",
        json={
            "user_id": user.user_id,
            "service_id": service.service_id,
            "event_name": "Test Event",
            "time_start": "13:00",
            "time_end": "15:00",
            "location": "123 Test St",
            "date_iso": "2026-05-01",
            "venue_latitude": 34.0,
            "venue_longitude": -118.0
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "pending"
    assert "booking_id" in data


def test_approve_booking(seeded_db):
    db = seeded_db["db"]
    user = seeded_db["user"]
    vendor = seeded_db["vendor"]
    service = seeded_db["service"]
    
    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        event_name="Auth Event Test", time_start="10:00", time_end="11:30", location="145 Main St",
        date_iso="2026-03-02", venue_latitude=40.0, venue_longitude=-70.0, status="pending"
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    # Approve as vendor
    response = client.put(
        f"/bookings/{booking.booking_id}/status",
        json={
            "user_id": vendor.user_id,
            "is_vendor": True,
            "status": "confirmed"
        }
    )
    assert response.status_code == 200
    assert response.json()["message"] == "Booking successfully confirmed"
    
    # Verify in DB
    db.refresh(booking)
    assert booking.status == "confirmed"


def test_client_cannot_approve(seeded_db):
    db = seeded_db["db"]
    user = seeded_db["user"]
    vendor = seeded_db["vendor"]
    service = seeded_db["service"]
    
    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        event_name="Client Try Approve", time_start="10:00", time_end="11:30", location="145 Main St",
        date_iso="2026-03-02", venue_latitude=40.0, venue_longitude=-70.0, status="pending"
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    # Try to approve as client
    response = client.put(
        f"/bookings/{booking.booking_id}/status",
        json={
            "user_id": user.user_id,
            "is_vendor": False,
            "status": "confirmed"
        }
    )
    assert response.status_code == 403


def test_get_user_bookings(seeded_db):
    user = seeded_db["user"]
    vendor = seeded_db["vendor"]
    service = seeded_db["service"]
    db = seeded_db["db"]
    
    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        event_name="Fetch Event", time_start="10:00", time_end="11:30", location="145 Main St",
        date_iso="2026-03-02", status="pending"
    )
    db.add(booking)
    db.commit()
    
    response = client.get(f"/bookings/user/{user.user_id}")
    assert response.status_code == 200
    assert len(response.json()["bookings"]) > 0


def test_get_vendor_bookings(seeded_db):
    user = seeded_db["user"]
    vendor = seeded_db["vendor"]
    service = seeded_db["service"]
    db = seeded_db["db"]
    
    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        event_name="Fetch Event", time_start="10:00", time_end="11:30", location="145 Main St",
        date_iso="2026-03-02", status="pending"
    )
    db.add(booking)
    db.commit()

    response = client.get(f"/bookings/vendor/{vendor.vendor_id}")
    assert response.status_code == 200
    assert len(response.json()["bookings"]) > 0
