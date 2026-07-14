"""
Tests for the notification system:
 - FCM token registration / removal / status endpoints
 - Notification wrapper functions (mocked Firebase)
 - Integration: booking creation and status updates trigger notifications
"""

import pytest
from unittest.mock import patch, MagicMock
from app.db.models import Booking, User, Service, Vendor
from app.models.schemas import BookingStatus
from tests.test_api import TestingSessionLocal, client, make_auth_headers
import uuid


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def seeded_db():
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    user = User(
        email=f"notif_client_{uid}@test.com",
        username=f"notif_client_{uid}",
        password="pw", phone="1", f_name="Priya", l_name="Patel",
        age=25, location="123", gender="F", language="EN",
        fcm_token="fake_client_fcm_token_123", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    vendor_user = User(
        email=f"notif_vendor_{uid}@test.com",
        username=f"notif_vendor_{uid}",
        password="pw", phone="1", f_name="Raj", l_name="Kumar",
        age=30, location="456", gender="M", language="HI",
        fcm_token="fake_vendor_fcm_token_456", token_version=0,
    )
    db.add(vendor_user)
    db.commit()
    db.refresh(vendor_user)

    vendor = Vendor(user_id=vendor_user.user_id, bio="Expert DJ", rating=4.9, num_events=100)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    service = Service(
        name="DJ Services", price=800.0, duration_minutes=240,
        vendor_id=vendor.vendor_id, experience="12 years",
    )
    db.add(service)
    db.commit()
    db.refresh(service)

    yield {
        "user": user,
        "vendor_user": vendor_user,
        "vendor": vendor,
        "service": service,
        "db": db,
    }
    db.close()


# ─────────────────────────────────────────────────────────────────────
# 1. FCM Token Registration Endpoints
# ─────────────────────────────────────────────────────────────────────

class TestFCMTokenEndpoints:

    def test_register_token(self, seeded_db):
        user = seeded_db["user"]
        headers = make_auth_headers(user)

        response = client.post(
            "/notifications/register-token",
            json={"fcm_token": "new_device_token_xyz"},
            headers=headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "FCM token registered successfully"
        assert data["user_id"] == user.user_id

        db = seeded_db["db"]
        db.refresh(user)
        assert user.fcm_token == "new_device_token_xyz"

    def test_register_token_requires_auth(self):
        response = client.post(
            "/notifications/register-token",
            json={"fcm_token": "some_token"},
        )
        assert response.status_code in (401, 403)

    def test_remove_token(self, seeded_db):
        user = seeded_db["user"]
        headers = make_auth_headers(user)

        response = client.delete(
            f"/notifications/remove-token/{user.user_id}",
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["message"] == "FCM token removed successfully"

        db = seeded_db["db"]
        db.refresh(user)
        assert user.fcm_token is None

    def test_remove_token_forbidden_for_other_user(self, seeded_db):
        user = seeded_db["user"]
        vendor_user = seeded_db["vendor_user"]
        headers = make_auth_headers(vendor_user)

        response = client.delete(
            f"/notifications/remove-token/{user.user_id}",
            headers=headers,
        )
        assert response.status_code == 403

    def test_token_status_has_token(self, seeded_db):
        user = seeded_db["user"]
        # Ensure token is set
        db = seeded_db["db"]
        user.fcm_token = "fake_client_fcm_token_123"
        db.commit()

        headers = make_auth_headers(user)
        response = client.get(
            f"/notifications/token-status/{user.user_id}",
            headers=headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == user.user_id
        assert data["has_token"] is True

    def test_token_status_no_token(self, seeded_db):
        user = seeded_db["user"]
        db = seeded_db["db"]
        user.fcm_token = None
        db.commit()

        headers = make_auth_headers(user)
        response = client.get(
            f"/notifications/token-status/{user.user_id}",
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["has_token"] is False

    def test_token_status_forbidden_for_other_user(self, seeded_db):
        user = seeded_db["user"]
        vendor_user = seeded_db["vendor_user"]
        headers = make_auth_headers(vendor_user)

        response = client.get(
            f"/notifications/token-status/{user.user_id}",
            headers=headers,
        )
        assert response.status_code == 403


# ─────────────────────────────────────────────────────────────────────
# 2. Notification Utility Functions (Unit Tests with Mocks)
# ─────────────────────────────────────────────────────────────────────

class TestNotificationUtils:

    def test_notify_booking_status_no_firebase(self):
        from app.utils.notifications import notify_booking_status_change
        result = notify_booking_status_change(
            status="pending",
            booking_id="test-booking-id",
            service_name="DJ",
            client_name="Priya Patel",
            vendor_name="Raj Kumar",
            client_fcm_token="token_client",
            vendor_fcm_token="token_vendor",
        )
        assert "client_result" in result
        assert "vendor_result" in result
        assert result["client_result"]["success"] is False
        assert result["vendor_result"]["success"] is False

    def test_notify_booking_no_tokens(self):
        from app.utils.notifications import notify_booking_status_change
        result = notify_booking_status_change(
            status="approved",
            booking_id="test-id",
            service_name="Photo",
            client_name="A",
            vendor_name="B",
            client_fcm_token=None,
            vendor_fcm_token=None,
        )
        assert result["client_result"]["error"] == "No client FCM token"
        assert result["vendor_result"]["error"] == "No vendor FCM token"

    def test_notify_booking_unknown_status(self):
        from app.utils.notifications import notify_booking_status_change
        result = notify_booking_status_change(
            status="some_unknown_status",
            booking_id="id",
            service_name="S",
            client_name="C",
            vendor_name="V",
        )
        assert "No template for status" in result["client_result"]["error"]
        assert "No template for status" in result["vendor_result"]["error"]

    def test_notify_checkin_no_token(self):
        from app.utils.notifications import notify_check_in
        result = notify_check_in(
            booking_id="bid",
            is_vendor=True,
            client_name="C",
            vendor_name="V",
            recipient_fcm_token=None,
        )
        assert result["success"] is False
        assert result["error"] == "No recipient FCM token"

    @patch("app.utils.notifications._ensure_firebase", return_value=True)
    @patch("app.utils.notifications.send_push_notification")
    def test_notify_booking_pending_calls_send(self, mock_send, mock_firebase):
        from app.utils.notifications import notify_booking_status_change
        mock_send.return_value = {"success": True, "message_id": "msg_123"}

        result = notify_booking_status_change(
            status="pending",
            booking_id="b1",
            service_name="DJ Services",
            client_name="Priya Patel",
            vendor_name="Raj Kumar",
            client_fcm_token="tok_c",
            vendor_fcm_token="tok_v",
        )
        assert mock_send.call_count == 2

        vendor_call = mock_send.call_args_list[0]
        assert vendor_call.kwargs["fcm_token"] == "tok_v"
        assert "New Booking Request" in vendor_call.kwargs["title"]
        assert "Priya Patel" in vendor_call.kwargs["body"]
        assert "DJ Services" in vendor_call.kwargs["body"]

        client_call = mock_send.call_args_list[1]
        assert client_call.kwargs["fcm_token"] == "tok_c"
        assert "Booking Submitted" in client_call.kwargs["title"]

    @patch("app.utils.notifications._ensure_firebase", return_value=True)
    @patch("app.utils.notifications.send_push_notification")
    def test_notify_booking_approved_templates(self, mock_send, mock_firebase):
        from app.utils.notifications import notify_booking_status_change
        mock_send.return_value = {"success": True, "message_id": "msg_456"}

        notify_booking_status_change(
            status="approved",
            booking_id="b2",
            service_name="Photography",
            client_name="Alice",
            vendor_name="Bob",
            client_fcm_token="c_tok",
            vendor_fcm_token="v_tok",
        )
        client_call = mock_send.call_args_list[1]
        assert "Booking Approved" in client_call.kwargs["title"]
        assert "Bob" in client_call.kwargs["body"]

    @patch("app.utils.notifications._ensure_firebase", return_value=True)
    @patch("app.utils.notifications.send_push_notification")
    def test_notify_booking_rejected_templates(self, mock_send, mock_firebase):
        from app.utils.notifications import notify_booking_status_change
        mock_send.return_value = {"success": True, "message_id": "msg_789"}

        notify_booking_status_change(
            status="rejected",
            booking_id="b3",
            service_name="Catering",
            client_name="X",
            vendor_name="Y",
            client_fcm_token="c",
            vendor_fcm_token="v",
        )
        client_call = mock_send.call_args_list[1]
        assert "Declined" in client_call.kwargs["title"]

    @patch("app.utils.notifications._ensure_firebase", return_value=True)
    @patch("app.utils.notifications.send_push_notification")
    def test_notify_booking_payment_confirmed_templates(self, mock_send, mock_firebase):
        from app.utils.notifications import notify_booking_status_change
        mock_send.return_value = {"success": True, "message_id": "msg_pay"}

        notify_booking_status_change(
            status="payment_confirmed",
            booking_id="b4",
            service_name="Sound System",
            client_name="P",
            vendor_name="Q",
            client_fcm_token="c",
            vendor_fcm_token="v",
        )
        client_call = mock_send.call_args_list[1]
        assert "Payment Confirmed" in client_call.kwargs["title"]

        vendor_call = mock_send.call_args_list[0]
        assert "Payment Received" in vendor_call.kwargs["title"]


# ─────────────────────────────────────────────────────────────────────
# 3. Integration: Bookings trigger notifications
# ─────────────────────────────────────────────────────────────────────

class TestBookingNotificationIntegration:

    def test_create_booking_includes_notification(self, seeded_db):
        user = seeded_db["user"]
        service = seeded_db["service"]
        headers = make_auth_headers(user)

        response = client.post(
            "/bookings",
            json={
                "service_id": service.service_id,
                "event_name": "Diwali Party",
                "time_start": "18:00",
                "time_end": "22:00",
                "location": "Community Hall",
                "date_iso": "2026-11-01",
            },
            headers=headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "pending"
        assert "notification" in data
        assert "client_result" in data["notification"]
        assert "vendor_result" in data["notification"]

    def test_approve_booking_includes_notification(self, seeded_db):
        db = seeded_db["db"]
        user = seeded_db["user"]
        vendor = seeded_db["vendor"]
        vendor_user = seeded_db["vendor_user"]
        service = seeded_db["service"]

        booking = Booking(
            user_id=user.user_id, vendor_id=vendor.vendor_id,
            service_id=service.service_id,            time_start="19:00", time_end="23:00", location="Park",
            date_iso="2026-10-15", status="pending",
        )
        db.add(booking)
        db.commit()
        db.refresh(booking)

        headers = make_auth_headers(vendor_user)
        response = client.put(
            f"/bookings/{booking.booking_id}/status",
            json={"is_vendor": True, "status": "approved"},
            headers=headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert "Booking successfully updated to approved" in data["message"]
        assert "notification" in data

    def test_reject_booking_includes_notification(self, seeded_db):
        db = seeded_db["db"]
        user = seeded_db["user"]
        vendor = seeded_db["vendor"]
        vendor_user = seeded_db["vendor_user"]
        service = seeded_db["service"]

        booking = Booking(
            user_id=user.user_id, vendor_id=vendor.vendor_id,
            service_id=service.service_id,            time_start="09:00", time_end="11:00", location="Venue",
            date_iso="2026-04-01", status="pending",
        )
        db.add(booking)
        db.commit()
        db.refresh(booking)

        headers = make_auth_headers(vendor_user)
        response = client.put(
            f"/bookings/{booking.booking_id}/status",
            json={"is_vendor": True, "status": "rejected"},
            headers=headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert "rejected" in data["message"]
        assert "notification" in data

    def test_checkin_includes_notification(self, seeded_db):
        db = seeded_db["db"]
        user = seeded_db["user"]
        vendor = seeded_db["vendor"]
        service = seeded_db["service"]

        booking = Booking(
            user_id=user.user_id, vendor_id=vendor.vendor_id,
            service_id=service.service_id,            time_start="10:00", time_end="12:00", location="Hall",
            date_iso="2026-06-01",
            venue_latitude=34.05, venue_longitude=-118.24,
            status="approved",
        )
        db.add(booking)
        db.commit()
        db.refresh(booking)

        headers = make_auth_headers(user)
        response = client.post(
            f"/bookings/{booking.booking_id}/check-in",
            json={"is_vendor": False, "latitude": 34.05, "longitude": -118.24},
            headers=headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Check-in successful."
        assert "notification" in data
