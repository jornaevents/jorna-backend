import pytest
from fastapi.testclient import TestClient
from datetime import datetime, timezone

from app.db.database import get_db
from app.db.models import Vendor, VendorAvailability, Booking, User, Service
from main import app
from tests.test_api import TestingSessionLocal, client

def test_google_auth_callback_success(mocker):
    """Test successful Google OAuth token exchange."""
    db = TestingSessionLocal()
    
    # Create test vendor
    user = User(email="oauth@test.com", username="oauth_test", password="pw", phone="1", f_name="A", l_name="B", age=20, location="123", gender="M", language="EN")
    db.add(user)
    db.commit()
    db.refresh(user)
    
    vendor = Vendor(user_id=user.user_id, bio="bio", rating=5.0, num_events=1)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    # Mock the Google OAuth Flow
    mock_flow = mocker.patch("app.routers.calendar.get_google_auth_flow")
    mock_flow_instance = mocker.MagicMock()
    mock_flow.return_value = mock_flow_instance
    mock_flow_instance.credentials.token = "fake_access_token"
    mock_flow_instance.credentials.refresh_token = "fake_refresh_token"

    response = client.get(f"/vendors/auth/callback?state={vendor.vendor_id}&code=auth_code_123")
    
    assert response.status_code == 200
    assert response.json() == {"message": "Google Calendar successfully connected"}
    
    # Verify DB was updated
    db.refresh(vendor)
    assert vendor.google_access_token == "fake_access_token"
    assert vendor.google_refresh_token == "fake_refresh_token"


def test_google_auth_callback_vendor_not_found(mocker):
    """Test Google OAuth callback when invalid state (vendor_id) is passed."""
    response = client.get("/vendors/auth/callback?state=invalid-uuid&code=auth_code_123")
    assert response.status_code == 404
    assert response.json()["detail"] == "Vendor not found"


def test_google_auth_callback_flow_error(mocker):
    """Test Google OAuth callback when token fetching fails (e.g. invalid code)."""
    db = TestingSessionLocal()
    vendor = db.query(Vendor).first() # Grab any existing vendor from previous tests

    mock_flow = mocker.patch("app.routers.calendar.get_google_auth_flow")
    mock_flow_instance = mocker.MagicMock()
    mock_flow_instance.fetch_token.side_effect = Exception("Invalid grant")
    mock_flow.return_value = mock_flow_instance

    response = client.get(f"/vendors/auth/callback?state={vendor.vendor_id}&code=bad_code")
    assert response.status_code == 400
    assert "Failed to fetch Google tokens" in response.json()["detail"]


def test_calendar_availability_aggregation(mocker):
    """Test the complex aggregation of baseline hours, internal bookings, and Google Calendar blocks."""
    db = TestingSessionLocal()
    
    # 1. Setup a specific Vendor for this test
    user = User(email="avail@test.com", username="avail_test", password="pw", phone="1", f_name="A", l_name="B", age=20, location="123", gender="M", language="EN")
    db.add(user)
    db.commit()
    db.refresh(user)
    
    vendor = Vendor(
        user_id=user.user_id, bio="bio", rating=5.0, num_events=1,
        google_access_token="test_token", google_refresh_token="test_refresh"
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    
    # 2. Add Vendor Availability Baseline Hours (e.g., Monday 9-5)
    baseline = VendorAvailability(vendor_id=vendor.vendor_id, day_of_week=0, start_time="09:00", end_time="17:00")
    db.add(baseline)
    db.commit()
    
    # 3. Add an Internal Desiconnect Booking
    service = Service(name="Test", price=100.0, duration_minutes=60, vendor_id=vendor.vendor_id, experience="none")
    db.add(service)
    db.commit()
    db.refresh(service)
    
    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        event_name="Event", time_start="10:00", time_end="11:30", location="123 Main St",
        date_iso="2026-03-02"  # Let's say March 2, 2026 is a Monday
    )
    db.add(booking)
    db.commit()

    # 4. Mock the Google API Client call
    mocker.patch("app.routers.calendar.create_google_calendar_service", return_value="mock_service")
    
    mock_google_api = mocker.patch("app.routers.calendar.get_freebusy_schedule")
    # Simulate Google Calendar returning one busy block (e.g. Doctor Appt from 1pm to 2pm)
    mock_google_api.return_value = [
        {"start": "2026-03-02T13:00:00Z", "end": "2026-03-02T14:00:00Z"}
    ]

    # Execute endpoint
    start_date = "2026-03-01T00:00:00Z"
    end_date = "2026-03-07T23:59:59Z"
    response = client.get(f"/vendors/{vendor.vendor_id}/availability?start_date={start_date}&end_date={end_date}")
    
    assert response.status_code == 200
    data = response.json()
    
    # Verify Baseline Hours
    assert "0" in data["baseline_hours_map"] # JSON stringifies dictionary integer keys
    assert data["baseline_hours_map"]["0"] == ["09:00", "17:00"]
    
    # Verify Internal Bookings were aggregated
    assert len(data["internal_busy_times"]) == 1
    assert data["internal_busy_times"][0]["start"] == "2026-03-02T10:00:00+00:00"
    assert data["internal_busy_times"][0]["end"] == "2026-03-02T11:30:00+00:00"
    
    # Verify Google Calendar was aggregated
    assert len(data["google_busy_times"]) == 1
    assert data["google_busy_times"][0]["start"] == "2026-03-02T13:00:00+00:00"


def test_calendar_availability_vendor_not_found():
    response = client.get("/vendors/invalid-uid/availability?start_date=2026-03-01T00:00:00Z&end_date=2026-03-07T23:59:59Z")
    assert response.status_code == 404
    assert response.json()["detail"] == "Vendor not found"


def test_check_in_no_coordinates():
    """Test check in fails gracefully when booking has no coordinates set."""
    db = TestingSessionLocal()
    user = db.query(User).first()
    vendor = db.query(Vendor).first()
    service = db.query(Service).first()
    
    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        event_name="Event No Coords", time_start="10:00", time_end="11:30", location="123 Main St",
        date_iso="2026-03-02" 
        # venue_latitude and venue_longitude purposefully missing
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    response = client.post(
        f"/bookings/{booking.booking_id}/check-in",
        json={"user_id": user.user_id, "is_vendor": False, "latitude": 40.0, "longitude": -70.0}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Booking has no venue coordinates set"


def test_check_in_unauthorized_user():
    """Test check in fails if a user tries to check in to someone else's booking."""
    db = TestingSessionLocal()
    user = db.query(User).first()
    vendor = db.query(Vendor).first()
    service = db.query(Service).first()
    
    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        event_name="Auth Event", time_start="10:00", time_end="11:30", location="145 Main St",
        date_iso="2026-03-02", venue_latitude=40.0, venue_longitude=-70.0
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    
    # Pass an entirely incorrect user_id
    response = client.post(
        f"/bookings/{booking.booking_id}/check-in",
        json={"user_id": "fake-user-id", "is_vendor": False, "latitude": booking.venue_latitude, "longitude": booking.venue_longitude}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Unauthorized user"
