"""
Tests for the notification system:
 - Push token registration / removal / status endpoints (push_tokens table)
 - send_push_to_user fan-out + dead-token pruning
 - Notification wrapper functions (mocked)
 - Integration: booking creation and status updates trigger notifications
"""

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.db.models import Booking, PushToken, User, Service, Vendor
from tests.test_api import TestingSessionLocal, client, make_auth_headers


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
        token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    vendor_user = User(
        email=f"notif_vendor_{uid}@test.com",
        username=f"notif_vendor_{uid}",
        password="pw", phone="1", f_name="Raj", l_name="Kumar",
        age=30, location="456", gender="M", language="HI",
        token_version=0,
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


def _add_token(db, user, token, platform="ios"):
    db.add(PushToken(user_id=user.user_id, token=token, platform=platform))
    db.commit()


# ─────────────────────────────────────────────────────────────────────
# 1. Push-token registration endpoints (push_tokens table)
# ─────────────────────────────────────────────────────────────────────

class TestPushTokenEndpoints:

    def test_register_token(self, seeded_db):
        user = seeded_db["user"]
        headers = make_auth_headers(user)

        response = client.post(
            "/notifications/register-token",
            json={"fcm_token": "new_device_token_xyz", "platform": "web"},
            headers=headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Push token registered successfully"
        assert data["user_id"] == user.user_id

        db = seeded_db["db"]
        row = db.query(PushToken).filter(PushToken.token == "new_device_token_xyz").first()
        assert row is not None
        assert row.user_id == user.user_id
        assert row.platform == "web"

    def test_register_token_defaults_to_ios(self, seeded_db):
        user = seeded_db["user"]
        headers = make_auth_headers(user)
        client.post(
            "/notifications/register-token",
            json={"fcm_token": f"tok_default_{user.user_id}"},
            headers=headers,
        )
        db = seeded_db["db"]
        row = db.query(PushToken).filter(PushToken.token == f"tok_default_{user.user_id}").first()
        assert row is not None and row.platform == "ios"

    def test_register_token_requires_auth(self):
        response = client.post(
            "/notifications/register-token",
            json={"fcm_token": "some_token"},
        )
        assert response.status_code in (401, 403)

    def test_remove_token(self, seeded_db):
        user, db = seeded_db["user"], seeded_db["db"]
        _add_token(db, user, f"tok_remove_{user.user_id}")

        headers = make_auth_headers(user)
        response = client.delete(
            f"/notifications/remove-token/{user.user_id}",
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["message"] == "Push token(s) removed successfully"

        remaining = db.query(PushToken).filter(PushToken.user_id == user.user_id).count()
        assert remaining == 0

    def test_remove_single_token_leaves_others(self, seeded_db):
        user, db = seeded_db["user"], seeded_db["db"]
        _add_token(db, user, f"tok_a_{user.user_id}")
        _add_token(db, user, f"tok_b_{user.user_id}", platform="web")

        headers = make_auth_headers(user)
        response = client.delete(
            f"/notifications/remove-token/{user.user_id}?token=tok_a_{user.user_id}",
            headers=headers,
        )
        assert response.status_code == 200
        tokens = {t.token for t in db.query(PushToken).filter(PushToken.user_id == user.user_id).all()}
        assert tokens == {f"tok_b_{user.user_id}"}

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
        user, db = seeded_db["user"], seeded_db["db"]
        _add_token(db, user, f"tok_status_{user.user_id}")

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
        user = seeded_db["user"]  # freshly created, no tokens
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
# 2. send_push_to_user — fan-out across devices + prune dead tokens
# ─────────────────────────────────────────────────────────────────────

class TestSendPushToUser:

    @patch("app.utils.notifications.send_push_notification")
    def test_fans_out_and_prunes_unregistered(self, mock_send, seeded_db):
        user, db = seeded_db["user"], seeded_db["db"]
        _add_token(db, user, f"live_{user.user_id}")
        _add_token(db, user, f"dead_{user.user_id}", platform="web")

        def _result(token, *_a, **_k):
            if token.startswith("dead_"):
                return {"success": False, "error": "Token unregistered"}
            return {"success": True, "message_id": "m"}

        mock_send.side_effect = _result

        from app.utils.notifications import send_push_to_user
        summary = send_push_to_user(user, "Hi", "there", {"k": "v"}, db=db)

        assert summary == {"sent": 1, "devices": 2}
        assert mock_send.call_count == 2
        # The unregistered token was pruned; the live one remains.
        remaining = {t.token for t in db.query(PushToken).filter(PushToken.user_id == user.user_id).all()}
        assert remaining == {f"live_{user.user_id}"}

    def test_no_devices_is_a_noop(self, seeded_db):
        user, db = seeded_db["user"], seeded_db["db"]
        from app.utils.notifications import send_push_to_user
        assert send_push_to_user(user, "t", "b", db=db) == {"sent": 0, "devices": 0}

    def test_none_user_is_a_noop(self, seeded_db):
        from app.utils.notifications import send_push_to_user
        assert send_push_to_user(None, "t", "b", db=seeded_db["db"]) == {"sent": 0, "devices": 0}


# ─────────────────────────────────────────────────────────────────────
# 3. Notification wrapper functions (mocked dispatch)
# ─────────────────────────────────────────────────────────────────────

def _fake_user(email="x@test.com"):
    return SimpleNamespace(user_id=str(uuid.uuid4()), email=email)


class TestNotificationUtils:

    def test_notify_booking_unknown_status(self):
        from app.utils.notifications import notify_booking_status_change
        result = notify_booking_status_change(
            status="some_unknown_status",
            booking_id="id",
            service_name="S",
            client_name="C",
            vendor_name="V",
            db=None,  # returns before touching the DB
        )
        assert "No template for status" in result["client_result"]["error"]
        assert "No template for status" in result["vendor_result"]["error"]

    @patch("app.utils.notifications.send_push_to_user")
    def test_notify_booking_no_devices(self, mock_push):
        mock_push.return_value = {"sent": 0, "devices": 0}
        from app.utils.notifications import notify_booking_status_change
        result = notify_booking_status_change(
            status="approved",
            booking_id="test-id",
            service_name="Photo",
            client_name="A",
            vendor_name="B",
            client_user=_fake_user(email=None),
            vendor_user=_fake_user(email=None),
            db=None,
        )
        assert result["client_result"]["sent"] == 0
        assert result["vendor_result"]["sent"] == 0

    @patch("app.utils.notifications.send_push_to_user")
    def test_notify_booking_pending_calls_send(self, mock_push):
        mock_push.return_value = {"sent": 1, "devices": 1}
        from app.utils.notifications import notify_booking_status_change

        client_user, vendor_user = _fake_user(), _fake_user()
        notify_booking_status_change(
            status="pending",
            booking_id="b1",
            service_name="DJ Services",
            client_name="Priya Patel",
            vendor_name="Raj Kumar",
            client_user=client_user,
            vendor_user=vendor_user,
            db=None,
        )
        assert mock_push.call_count == 2

        # notify_ pushes the vendor first, then the client.
        vendor_call = mock_push.call_args_list[0]
        assert vendor_call.args[0] is vendor_user
        assert "New Booking Request" in vendor_call.args[1]
        assert "Priya Patel" in vendor_call.args[2]
        assert "DJ Services" in vendor_call.args[2]

        client_call = mock_push.call_args_list[1]
        assert client_call.args[0] is client_user
        assert "Booking Submitted" in client_call.args[1]

    @patch("app.utils.notifications.send_push_to_user")
    def test_notify_booking_approved_templates(self, mock_push):
        mock_push.return_value = {"sent": 1, "devices": 1}
        from app.utils.notifications import notify_booking_status_change
        notify_booking_status_change(
            status="approved", booking_id="b2", service_name="Photography",
            client_name="Alice", vendor_name="Bob",
            client_user=_fake_user(), vendor_user=_fake_user(), db=None,
        )
        client_call = mock_push.call_args_list[1]
        assert "Booking Approved" in client_call.args[1]
        assert "Bob" in client_call.args[2]

    @patch("app.utils.notifications.send_push_to_user")
    def test_notify_booking_rejected_templates(self, mock_push):
        mock_push.return_value = {"sent": 1, "devices": 1}
        from app.utils.notifications import notify_booking_status_change
        notify_booking_status_change(
            status="rejected", booking_id="b3", service_name="Catering",
            client_name="X", vendor_name="Y",
            client_user=_fake_user(), vendor_user=_fake_user(), db=None,
        )
        assert "Declined" in mock_push.call_args_list[1].args[1]

    @patch("app.utils.notifications.send_push_to_user")
    def test_notify_booking_payment_confirmed_templates(self, mock_push):
        mock_push.return_value = {"sent": 1, "devices": 1}
        from app.utils.notifications import notify_booking_status_change
        notify_booking_status_change(
            status="payment_confirmed", booking_id="b4", service_name="Sound System",
            client_name="P", vendor_name="Q",
            client_user=_fake_user(), vendor_user=_fake_user(), db=None,
        )
        assert "Payment Confirmed" in mock_push.call_args_list[1].args[1]
        assert "Payment Received" in mock_push.call_args_list[0].args[1]

    @patch("app.utils.notifications.send_push_to_user")
    def test_notify_checkin_calls_send(self, mock_push):
        mock_push.return_value = {"sent": 1, "devices": 1}
        from app.utils.notifications import notify_check_in
        recipient = _fake_user()
        notify_check_in(
            booking_id="bid", is_vendor=True,
            client_name="C", vendor_name="V",
            recipient_user=recipient, db=None,
        )
        call = mock_push.call_args_list[0]
        assert call.args[0] is recipient

    def test_notify_checkin_no_recipient(self):
        from app.utils.notifications import notify_check_in
        result = notify_check_in(
            booking_id="bid", is_vendor=True,
            client_name="C", vendor_name="V",
            recipient_user=None, db=None,
        )
        assert result["sent"] == 0


# ─────────────────────────────────────────────────────────────────────
# 4. Integration: bookings trigger notifications
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
        # No bundle given, so this booking opened its own — as a draft. A draft
        # is the client still assembling, and nobody is told until they send.
        assert data["notification"] == {
            "skipped": "Draft plan — vendors are told when it's sent."
        }

    def test_booking_into_a_sent_plan_does_notify(self, seeded_db):
        """Those vendors are already waiting on this plan, and it could not have
        been sent without the details — so a service added to it goes out now."""
        from datetime import datetime, timezone
        from app.db.models import Bundle

        db = seeded_db["db"]
        user = seeded_db["user"]
        service = seeded_db["service"]
        now = datetime.now(timezone.utc)
        bundle = Bundle(user_id=user.user_id, name="Live plan", status="confirmed",
                        created_at=now, updated_at=now)
        db.add(bundle)
        db.commit()
        db.refresh(bundle)

        response = client.post(
            "/bookings",
            json={
                "service_id": service.service_id,
                "event_name": "Diwali Party",
                "time_start": "18:00",
                "time_end": "22:00",
                "location": "Community Hall",
                "date_iso": "2026-11-02",
                "bundle_id": bundle.bundle_id,
            },
            headers=make_auth_headers(user),
        )
        assert response.status_code == 200
        notification = response.json()["notification"]
        assert "client_result" in notification
        assert "vendor_result" in notification

    def test_approve_booking_includes_notification(self, seeded_db):
        db = seeded_db["db"]
        user = seeded_db["user"]
        vendor = seeded_db["vendor"]
        vendor_user = seeded_db["vendor_user"]
        service = seeded_db["service"]

        booking = Booking(
            user_id=user.user_id, vendor_id=vendor.vendor_id,
            service_id=service.service_id, time_start="19:00", time_end="23:00", location="Park",
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
            service_id=service.service_id, time_start="09:00", time_end="11:00", location="Venue",
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
            service_id=service.service_id, time_start="10:00", time_end="12:00", location="Hall",
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
