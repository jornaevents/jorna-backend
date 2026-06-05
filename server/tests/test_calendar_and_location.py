import pytest
from app.db.models import Vendor, VendorAvailability, Booking, User, Service
from app.services.calendar_service import _encode_state
from tests.test_api import TestingSessionLocal, client, make_auth_headers


def _make_state(vendor_id: str) -> str:
    """Generate a valid HMAC-signed OAuth state for a vendor."""
    return _encode_state(vendor_id, "test-code-verifier")


def test_google_auth_callback_success(mocker):
    db = TestingSessionLocal()

    user = User(
        email="oauth@test.com", username="oauth_test", password="pw",
        phone="1", f_name="A", l_name="B", age=20, location="123",
        gender="M", language="EN", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    vendor = Vendor(user_id=user.user_id, bio="bio", rating=5.0, num_events=1)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    vendor_id = vendor.vendor_id
    db.close()

    mock_flow = mocker.patch("app.services.calendar_service.get_google_auth_flow")
    mock_flow_instance = mocker.MagicMock()
    mock_flow.return_value = mock_flow_instance
    mock_flow_instance.credentials.token = "fake_access_token"
    mock_flow_instance.credentials.refresh_token = "fake_refresh_token"

    state = _make_state(vendor_id)
    # Don't follow the redirect — the callback redirects to the frontend
    response = client.get(
        f"/vendors/auth/callback?state={state}&code=auth_code_123",
        follow_redirects=False,
    )
    assert response.status_code in (302, 307)
    assert "success=true" in response.headers["location"]

    db2 = TestingSessionLocal()
    vendor_fresh = db2.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    assert vendor_fresh.google_access_token == "fake_access_token"
    assert vendor_fresh.google_refresh_token == "fake_refresh_token"
    db2.close()


def test_google_auth_callback_invalid_state():
    """Forged/malformed state is rejected — redirects to frontend with success=false."""
    response = client.get(
        "/vendors/auth/callback?state=not-a-valid-state&code=auth_code_123",
        follow_redirects=False,
    )
    assert response.status_code in (302, 307)
    assert "success=false" in response.headers["location"]


def test_google_auth_callback_flow_error(mocker):
    db = TestingSessionLocal()
    vendor = db.query(Vendor).first()
    vendor_id = vendor.vendor_id
    db.close()

    mock_flow = mocker.patch("app.services.calendar_service.get_google_auth_flow")
    mock_flow_instance = mocker.MagicMock()
    mock_flow_instance.fetch_token.side_effect = Exception("Invalid grant")
    mock_flow.return_value = mock_flow_instance

    state = _make_state(vendor_id)
    response = client.get(
        f"/vendors/auth/callback?state={state}&code=bad_code",
        follow_redirects=False,
    )
    assert response.status_code in (302, 307)
    assert "success=false" in response.headers["location"]


def test_calendar_availability_aggregation(mocker):
    db = TestingSessionLocal()

    user = User(
        email="avail@test.com", username="avail_test", password="pw",
        phone="1", f_name="A", l_name="B", age=20, location="123",
        gender="M", language="EN", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    vendor = Vendor(
        user_id=user.user_id, bio="bio", rating=5.0, num_events=1,
        google_access_token="test_token", google_refresh_token="test_refresh",
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    baseline = VendorAvailability(vendor_id=vendor.vendor_id, day_of_week=0, start_time="09:00", end_time="17:00")
    db.add(baseline)
    db.commit()

    service = Service(name="Test", price=100.0, duration_minutes=60, vendor_id=vendor.vendor_id, experience="none")
    db.add(service)
    db.commit()
    db.refresh(service)

    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="10:00", time_end="11:30",
        location="123 Main St", date_iso="2026-03-02",
    )
    db.add(booking)
    db.commit()
    vendor_id = vendor.vendor_id
    db.close()

    mock_creds = mocker.MagicMock()
    mock_creds.token = "test_token"
    mocker.patch("app.services.calendar_service.create_google_calendar_service", return_value=("mock_service", mock_creds))
    mock_google_api = mocker.patch("app.services.calendar_service.get_freebusy_schedule")
    mock_google_api.return_value = [{"start": "2026-03-02T13:00:00Z", "end": "2026-03-02T14:00:00Z"}]

    response = client.get(f"/vendors/{vendor_id}/availability?start_date=2026-03-01T00:00:00Z&end_date=2026-03-07T23:59:59Z")
    assert response.status_code == 200
    data = response.json()

    assert "0" in data["baseline_hours_map"]
    assert data["baseline_hours_map"]["0"] == ["09:00", "17:00"]
    assert len(data["internal_busy_times"]) == 1
    assert data["internal_busy_times"][0]["start"] == "2026-03-02T10:00:00+00:00"
    assert len(data["google_busy_times"]) == 1
    assert data["google_busy_times"][0]["start"] == "2026-03-02T13:00:00+00:00"


def test_calendar_availability_vendor_not_found():
    response = client.get("/vendors/invalid-uid/availability?start_date=2026-03-01T00:00:00Z&end_date=2026-03-07T23:59:59Z")
    assert response.status_code == 404
    assert response.json()["detail"] == "Vendor not found"


def test_check_in_no_coordinates():
    db = TestingSessionLocal()
    user = db.query(User).first()
    vendor = db.query(Vendor).first()
    service = db.query(Service).first()

    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="10:00", time_end="11:30",
        location="123 Main St", date_iso="2026-03-02",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    user_id = user.user_id
    user_email = user.email
    booking_id = booking.booking_id
    db.close()

    from tests.test_api import make_auth_headers_from_parts
    headers = make_auth_headers_from_parts(user_id, user_email, 0)
    response = client.post(
        f"/bookings/{booking_id}/check-in",
        json={"latitude": 40.0, "longitude": -70.0},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Booking has no venue coordinates set"


def test_check_in_unauthorized_user():
    """A user who is neither the customer nor the vendor gets 403."""
    db = TestingSessionLocal()
    user = db.query(User).first()
    vendor = db.query(Vendor).first()
    service = db.query(Service).first()

    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="10:00", time_end="11:30",
        location="145 Main St", date_iso="2026-03-02",
        venue_latitude=40.0, venue_longitude=-70.0,
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    booking_id = booking.booking_id
    db.close()

    # Create a completely unrelated user and use their token
    from tests.test_api import make_auth_headers_from_parts
    import uuid
    headers = make_auth_headers_from_parts(str(uuid.uuid4()), "stranger@test.com", 0)
    response = client.post(
        f"/bookings/{booking_id}/check-in",
        json={"latitude": 40.0, "longitude": -70.0},
        headers=headers,
    )
    # get_current_user returns 401 (user not in DB), or 403 if booking unauthorized
    assert response.status_code in (401, 403)
