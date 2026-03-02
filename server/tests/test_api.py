import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base, get_db
from app.db.models import User, Vendor, Service, Booking
from main import app

import os

# Create a test sqlite database file (so multiple connections see the same DB)
SQLALCHEMY_DATABASE_URL = "sqlite:///./test.db"

# Remove the test db if it already exists
if os.path.exists("./test.db"):
    os.remove("./test.db")

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base.metadata.create_all(bind=engine)

def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)

@pytest.fixture(scope="session", autouse=True)
def setup_data():
    """Setup and teardown the DB."""
    yield
    # Cleanup after all tests
    if os.path.exists("./test.db"):
        os.remove("./test.db")


def test_db_check():
    response = client.get("/db-check")
    assert response.status_code == 200
    assert response.json() == {"db": "connected"}


def test_register_user():
    response = client.post(
        "/auth/register",
        json={
            "email": "test@example.com",
            "password": "password123",
            "username": "tester",
            "phone": "1234567890",
            "f_name": "Test",
            "l_name": "User",
            "age": 25,
            "location": "10001",
            "gender": "Test Gender",
            "language": "English",
            "latitude": 40.7128,
            "longitude": -74.0060,
            "city": "New York",
            "state": "NY"
        }
    )
    assert response.status_code == 200
    data = response.json()
    assert "user_id" in data
    assert data["email"] == "test@example.com"


def test_register_duplicate_user():
    response = client.post(
        "/auth/register",
        json={
            "email": "test@example.com",
            "password": "password123",
            "username": "tester",
            "phone": "1234567890",
            "f_name": "Test",
            "l_name": "User",
            "age": 25,
            "location": "10001",
            "gender": "Test Gender",
            "language": "English"
        }
    )
    assert response.status_code == 400


def test_login_user():
    response = client.post(
        "/auth/login",
        json={
            "email": "test@example.com",
            "password": "password123"
        }
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_vendors_search():
    # Insert vendor, user and service directly into DB to test search
    db = TestingSessionLocal()
    
    # First user
    user = User(
        email="vendor_search@test.com", username="vendor_search", password="pw",
        phone="12", f_name="Jane", l_name="Doe", age=30, location="123",
        gender="F", language="EN", latitude=34.05, longitude=-118.24, # LA 
        city="LA", state="CA"
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    
    vendor = Vendor(
        user_id=user.user_id, bio="Test Bio", rating=4.5, num_events=10, 
        travel_radius_miles=50
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    
    service = Service(
        name="Photography", price=500.0, duration_minutes=120, 
        vendor_id=vendor.vendor_id, experience="5 years"
    )
    db.add(service)
    db.commit()
    
    # Test searching 1 mile away from the vendor (should appear)
    response = client.get("/vendors/search?service_name=Photo&latitude=34.055&longitude=-118.24")
    assert response.status_code == 200
    data = response.json()
    assert len(data["vendors"]) == 1
    assert data["vendors"][0]["first_name"] == "Jane"
    
    # Test searching 100 miles away (should be excluded by 50 mile radius)
    response = client.get("/vendors/search?service_name=Photo&latitude=35.5&longitude=-118.24")
    assert response.status_code == 200
    assert len(response.json()["vendors"]) == 0
    

def test_google_auth_redirect():
    # Insert a dummy vendor for testing auth link
    db = TestingSessionLocal()
    user = User(
        email="vendor_cal@test.com", username="vendor_cal", password="pw",
        phone="1", f_name="Cal", l_name="Cal", age=20, location="123",
        gender="M", language="EN"
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    vendor = Vendor(
        user_id=user.user_id, bio="bio", rating=5.0, num_events=1
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    
    response = client.get(f"/vendors/{vendor.vendor_id}/google-auth")
    assert response.status_code == 200
    data = response.json()
    assert "https://accounts.google.com/o/oauth2/auth" in data["auth_url"]
    assert vendor.vendor_id in data["auth_url"]


def test_booking_check_in():
    db = TestingSessionLocal()
    # Need a booking in memory DB
    user = User(
        email="client@test.com", username="client", password="pw", phone="1", 
        f_name="C", l_name="C", age=20, location="123", gender="M", language="EN"
    )
    vendor_user = User(
         email="v_user@test.com", username="v_user", password="pw", phone="1", 
        f_name="V", l_name="V", age=20, location="123", gender="M", language="EN"
    )
    db.add(user)
    db.add(vendor_user)
    db.commit()
    db.refresh(user)
    db.refresh(vendor_user)
    
    vendor = Vendor(user_id=vendor_user.user_id, bio="bio", rating=5.0, num_events=1)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    
    service = Service(
        name="Test", price=100.0, duration_minutes=60, vendor_id=vendor.vendor_id, experience="none"
    )
    db.add(service)
    db.commit()
    db.refresh(service)
    
    booking = Booking(
        user_id=user.user_id,
        vendor_id=vendor.vendor_id,
        service_id=service.service_id,
        event_name="Wedding",
        time_start="10:00",
        time_end="12:00",
        location="123 Main St",
        date_iso="2026-03-01",
        venue_latitude=40.0,
        venue_longitude=-70.0
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    
    # Check in from 10 miles away -> should fail
    response = client.post(
        f"/bookings/{booking.booking_id}/check-in",
        json={
            "user_id": user.user_id,
            "is_vendor": False,
            "latitude": 40.1,  # Way too far
            "longitude": -70.0
        }
    )
    assert response.status_code == 400
    assert "You must be at the venue to check in" in response.json()["detail"]
    
    # Check in from EXACT location -> should pass
    response = client.post(
        f"/bookings/{booking.booking_id}/check-in",
        json={
            "user_id": user.user_id,
            "is_vendor": False,
            "latitude": 40.0,
            "longitude": -70.0  
        }
    )
    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "Check-in successful"
    assert "check_in_time" in data
