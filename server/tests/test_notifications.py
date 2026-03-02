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
from tests.test_api import TestingSessionLocal, client
import uuid


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def seeded_db():
    """Create a user, vendor, and service for notification tests."""
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    user = User(
        email=f"notif_client_{uid}@test.com",
        username=f"notif_client_{uid}",
        password="pw", phone="1", f_name="Priya", l_name="Patel",
        age=25, location="123", gender="F", language="EN",
        fcm_token="fake_client_fcm_token_123",
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    vendor_user = User(
        email=f"notif_vendor_{uid}@test.com",
        username=f"notif_vendor_{uid}",
        password="pw", phone="1", f_name="Raj", l_name="Kumar",
        age=30, location="456", gender="M", language="HI",
        fcm_token="fake_vendor_fcm_token_456",
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
        vendor_id=vendor.vendor_id, experience="12 years"
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
    """Test the /notifications/* REST endpoints."""

    def test_register_token(self, seeded_db):
        user = seeded_db["user"]
        response = client.post(
            "/notifications/register-token",
            json={"user_id": user.user_id, "fcm_token": "new_device_token_xyz"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "FCM token registered successfully"
        assert data["user_id"] == user.user_id

        # Verify in DB
        db = seeded_db["db"]
        db.refresh(user)
        assert user.fcm_token == "new_device_token_xyz"

    def test_register_token_user_not_found(self):
        response = client.post(
            "/notifications/register-token",
            json={"user_id": "nonexistent-user-id", "fcm_token": "some_token"},
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "User not found"

    def test_remove_token(self, seeded_db):
        user = seeded_db["user"]
        response = client.delete(f"/notifications/remove-token/{user.user_id}")
        assert response.status_code == 200
        assert response.json()["message"] == "FCM token removed successfully"

        # Verify in DB
        db = seeded_db["db"]
        db.refresh(user)
        assert user.fcm_token is None

    def test_remove_token_user_not_found(self):
        response = client.delete("/notifications/remove-token/fake-user-id")
        assert response.status_code == 404

    def test_token_status_has_token(self, seeded_db):
        user = seeded_db["user"]
        response = client.get(f"/notifications/token-status/{user.user_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == user.user_id
        assert data["has_token"] is True

    def test_token_status_no_token(self, seeded_db):
        user = seeded_db["user"]
        db = seeded_db["db"]
        user.fcm_token = None
        db.commit()

        response = client.get(f"/notifications/token-status/{user.user_id}")
        assert response.status_code == 200
        assert response.json()["has_token"] is False

    def test_token_status_user_not_found(self):
        response = client.get("/notifications/token-status/fake-user-id")
        assert response.status_code == 404


# ─────────────────────────────────────────────────────────────────────
# 2. Notification Utility Functions (Unit Tests with Mocks)
# ─────────────────────────────────────────────────────────────────────

class TestNotificationUtils:
    """Unit-test the notification wrapper functions themselves."""

    def test_notify_booking_status_no_firebase(self):
        """When Firebase is not configured, should return error gracefully."""
        from app.utils.notifications import notify_booking_status_change

        result = notify_booking_status_change(
            status="pending",
            booking_id="test-booking-id",
            event_name="Wedding",
            service_name="DJ",
            client_name="Priya Patel",
            vendor_name="Raj Kumar",
            client_fcm_token="token_client",
            vendor_fcm_token="token_vendor",
        )
        # Firebase creds not present → both should fail gracefully
        assert "client_result" in result
        assert "vendor_result" in result
        assert result["client_result"]["success"] is False
        assert result["vendor_result"]["success"] is False

    def test_notify_booking_no_tokens(self):
        """When no FCM tokens are provided, skip silently."""
        from app.utils.notifications import notify_booking_status_change

        result = notify_booking_status_change(
            status="approved",
            booking_id="test-id",
            event_name="Event",
            service_name="Photo",
            client_name="A",
            vendor_name="B",
            client_fcm_token=None,
            vendor_fcm_token=None,
        )
        assert result["client_result"]["error"] == "No client FCM token"
        assert result["vendor_result"]["error"] == "No vendor FCM token"

    def test_notify_booking_unknown_status(self):
        """Unknown status string should return a template-not-found error."""
        from app.utils.notifications import notify_booking_status_change

        result = notify_booking_status_change(
            status="some_unknown_status",
            booking_id="id",
            event_name="E",
            service_name="S",
            client_name="C",
            vendor_name="V",
        )
        assert "No template for status" in result["client_result"]["error"]
        assert "No template for status" in result["vendor_result"]["error"]

    def test_notify_checkin_no_token(self):
        """Check-in notification with no recipient token should fail gracefully."""
        from app.utils.notifications import notify_check_in

        result = notify_check_in(
            booking_id="bid",
            event_name="Wedding",
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
        """When Firebase IS configured, the right templates are used for 'pending'."""
        from app.utils.notifications import notify_booking_status_change

        mock_send.return_value = {"success": True, "message_id": "msg_123"}

        result = notify_booking_status_change(
            status="pending",
            booking_id="b1",
            event_name="Sangeet",
            service_name="DJ Services",
            client_name="Priya Patel",
            vendor_name="Raj Kumar",
            client_fcm_token="tok_c",
            vendor_fcm_token="tok_v",
        )

        assert mock_send.call_count == 2

        # Check vendor call
        vendor_call = mock_send.call_args_list[0]
        assert vendor_call.kwargs["fcm_token"] == "tok_v"
        assert "New Booking Request" in vendor_call.kwargs["title"]
        assert "Priya Patel" in vendor_call.kwargs["body"]
        assert "DJ Services" in vendor_call.kwargs["body"]

        # Check client call
        client_call = mock_send.call_args_list[1]
        assert client_call.kwargs["fcm_token"] == "tok_c"
        assert "Booking Submitted" in client_call.kwargs["title"]

    @patch("app.utils.notifications._ensure_firebase", return_value=True)
    @patch("app.utils.notifications.send_push_notification")
    def test_notify_booking_approved_templates(self, mock_send, mock_firebase):
        """Verify the approved status uses the correct template text."""
        from app.utils.notifications import notify_booking_status_change

        mock_send.return_value = {"success": True, "message_id": "msg_456"}

        notify_booking_status_change(
            status="approved",
            booking_id="b2",
            event_name="Reception",
            service_name="Photography",
            client_name="Alice",
            vendor_name="Bob",
            client_fcm_token="c_tok",
            vendor_fcm_token="v_tok",
        )

        client_call = mock_send.call_args_list[1]
        assert "Booking Approved" in client_call.kwargs["title"]
        assert "Bob" in client_call.kwargs["body"]  # vendor name in client msg

    @patch("app.utils.notifications._ensure_firebase", return_value=True)
    @patch("app.utils.notifications.send_push_notification")
    def test_notify_booking_rejected_templates(self, mock_send, mock_firebase):
        from app.utils.notifications import notify_booking_status_change

        mock_send.return_value = {"success": True, "message_id": "msg_789"}

        notify_booking_status_change(
            status="rejected",
            booking_id="b3",
            event_name="Party",
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
            event_name="Garba Night",
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
    """
    End-to-end tests verifying that creating/updating bookings
    returns notification results in the response, even when Firebase
    is not configured (graceful degradation).
    """

    def test_create_booking_includes_notification(self, seeded_db):
        user = seeded_db["user"]
        service = seeded_db["service"]

        response = client.post(
            "/bookings",
            json={
                "user_id": user.user_id,
                "service_id": service.service_id,
                "event_name": "Diwali Party",
                "time_start": "18:00",
                "time_end": "22:00",
                "location": "Community Hall",
                "date_iso": "2026-11-01",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "pending"
        assert "notification" in data
        # Firebase not configured → results should indicate failure gracefully
        assert "client_result" in data["notification"]
        assert "vendor_result" in data["notification"]

    def test_approve_booking_includes_notification(self, seeded_db):
        db = seeded_db["db"]
        user = seeded_db["user"]
        vendor = seeded_db["vendor"]
        service = seeded_db["service"]

        booking = Booking(
            user_id=user.user_id, vendor_id=vendor.vendor_id,
            service_id=service.service_id, event_name="Navratri",
            time_start="19:00", time_end="23:00", location="Park",
            date_iso="2026-10-15", status="pending",
        )
        db.add(booking)
        db.commit()
        db.refresh(booking)

        response = client.put(
            f"/bookings/{booking.booking_id}/status",
            json={
                "user_id": seeded_db["vendor_user"].user_id,
                "is_vendor": True,
                "status": "approved",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "Booking successfully updated to approved" in data["message"]
        assert "notification" in data

    def test_reject_booking_includes_notification(self, seeded_db):
        db = seeded_db["db"]
        user = seeded_db["user"]
        vendor = seeded_db["vendor"]
        service = seeded_db["service"]

        booking = Booking(
            user_id=user.user_id, vendor_id=vendor.vendor_id,
            service_id=service.service_id, event_name="Reject Test",
            time_start="09:00", time_end="11:00", location="Venue",
            date_iso="2026-04-01", status="pending",
        )
        db.add(booking)
        db.commit()
        db.refresh(booking)

        response = client.put(
            f"/bookings/{booking.booking_id}/status",
            json={
                "user_id": seeded_db["vendor_user"].user_id,
                "is_vendor": True,
                "status": "rejected",
            },
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
            service_id=service.service_id, event_name="Check-In Test",
            time_start="10:00", time_end="12:00", location="Hall",
            date_iso="2026-06-01",
            venue_latitude=34.05, venue_longitude=-118.24,
            status="approved",
        )
        db.add(booking)
        db.commit()
        db.refresh(booking)

        response = client.post(
            f"/bookings/{booking.booking_id}/check-in",
            json={
                "user_id": user.user_id,
                "is_vendor": False,
                "latitude": 34.05,
                "longitude": -118.24,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Check-in successful"
        assert "notification" in data
