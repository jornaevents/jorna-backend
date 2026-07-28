import os
import pytest

# Set required env vars before importing the app
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only-32chars!!")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_dummy")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base, get_db
from app.db.models import User, Vendor, Service, Booking
from main import app

SQLALCHEMY_DATABASE_URL = "sqlite:///./test.db"

if os.path.exists("./test.db"):
    os.remove("./test.db")

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
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


def make_auth_headers(user: User) -> dict:
    """Generate a valid JWT for a test user and return Authorization headers."""
    import jwt
    from datetime import datetime, timedelta, timezone
    token = jwt.encode(
        {
            "sub": user.user_id,
            "email": user.email,
            "tv": user.token_version,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        os.environ["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def make_auth_headers_from_parts(user_id: str, email: str, token_version: int) -> dict:
    """Generate auth headers from raw values (use when session is already closed)."""
    import jwt
    from datetime import datetime, timedelta, timezone
    token = jwt.encode(
        {
            "sub": user_id,
            "email": email,
            "tv": token_version,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        os.environ["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session", autouse=True)
def setup_data():
    yield
    engine.dispose()
    if os.path.exists("./test.db"):
        os.remove("./test.db")


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_register_user():
    response = client.post(
        "/auth/register",
        json={
            "email": "test@example.com",
            "password": "Password123!",
            "username": "tester",
            "phone": "1234567890",
            "f_name": "Test",
            "l_name": "User",
            "age": 25,
            "location": "10001",
            "gender": "Test Gender",
            "language": "English",
        },
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
            "password": "Password123!",
            "username": "tester",
            "phone": "1234567890",
            "f_name": "Test",
            "l_name": "User",
            "age": 25,
            "location": "10001",
            "gender": "Test Gender",
            "language": "English",
        },
    )
    assert response.status_code == 400


def test_login_user():
    response = client.post(
        "/auth/login",
        json={"identifier": "test@example.com", "password": "Password123!"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_vendors_search():
    db = TestingSessionLocal()

    user = User(
        email="vendor_search@test.com", username="vendor_search", password="pw",
        phone="12", f_name="Jane", l_name="Doe", age=30, location="123",
        gender="F", language="EN", latitude=34.05, longitude=-118.24,
        city="LA", state="CA", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    vendor = Vendor(user_id=user.user_id, bio="Test Bio", rating=4.5, num_events=10, travel_radius_miles=50)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    service = Service(name="Photography", price=500.0, duration_minutes=120, vendor_id=vendor.vendor_id, experience="5 years")
    db.add(service)
    db.commit()
    db.close()

    # 1 mile away — should appear
    response = client.get("/vendors/search?service_name=Photo&latitude=34.055&longitude=-118.24")
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["first_name"] == "Jane"
    assert "total" in data

    # 100 miles away — excluded by 50-mile radius
    response = client.get("/vendors/search?service_name=Photo&latitude=35.5&longitude=-118.24")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 0


@pytest.mark.skipif(
    not os.path.exists(os.environ.get("GOOGLE_CLIENT_SECRETS_FILE", "client_secret.json")),
    reason="client_secret.json not configured — Google OAuth URL generation unavailable",
)
def test_google_auth_redirect():
    db = TestingSessionLocal()
    user = User(
        email="vendor_cal@test.com", username="vendor_cal", password="pw",
        phone="1", f_name="Cal", l_name="Cal", age=20, location="123",
        gender="M", language="EN", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    vendor = Vendor(user_id=user.user_id, bio="bio", rating=5.0, num_events=1)
    db.add(vendor)
    db.commit()
    vendor_id = vendor.vendor_id
    headers = make_auth_headers(user)
    db.close()

    # Signed in: the URL this returns is a valid authorization for the vendor
    # named in its state, so it isn't handed to anonymous callers.
    response = client.get(f"/vendors/{vendor_id}/google-auth", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "https://accounts.google.com/o/oauth2/auth" in data["auth_url"]
    # vendor_id is encoded in the HMAC-signed state param, not in the URL directly
    assert "state=" in data["auth_url"]


def test_booking_check_in():
    db = TestingSessionLocal()

    user = User(
        email="client@test.com", username="client", password="pw", phone="1",
        f_name="C", l_name="C", age=20, location="123", gender="M", language="EN",
        token_version=0,
    )
    vendor_user = User(
        email="v_user@test.com", username="v_user", password="pw", phone="1",
        f_name="V", l_name="V", age=20, location="123", gender="M", language="EN",
        token_version=0,
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

    service = Service(name="Test", price=100.0, duration_minutes=60, vendor_id=vendor.vendor_id, experience="none")
    db.add(service)
    db.commit()
    db.refresh(service)

    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="10:00", time_end="12:00",
        location="123 Main St", date_iso="2026-03-01",
        venue_latitude=40.0, venue_longitude=-70.0,
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    # Capture IDs before closing session to avoid DetachedInstanceError
    user_id = user.user_id
    user_email = user.email
    booking_id = booking.booking_id
    db.close()

    headers = make_auth_headers_from_parts(user_id, user_email, 0)

    # Too far away — should fail
    response = client.post(
        f"/bookings/{booking_id}/check-in",
        json={"latitude": 40.1, "longitude": -70.0},
        headers=headers,
    )
    assert response.status_code == 400
    assert "You must be at the venue to check in" in response.json()["detail"]

    # Exact location — should pass
    response = client.post(
        f"/bookings/{booking_id}/check-in",
        json={"latitude": 40.0, "longitude": -70.0},
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "Check-in successful."
    assert "check_in_time" in data
