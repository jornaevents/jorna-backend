"""Tests for the Resend email service and the booking-notification email fallback."""

from unittest.mock import MagicMock, patch


# ─────────────────────────────────────────────────────────────────────
# email_service.send_email
# ─────────────────────────────────────────────────────────────────────

class TestSendEmail:
    def test_no_recipient(self):
        from app.services.email_service import send_email
        result = send_email(to="", subject="Hi", html="<p>x</p>")
        assert result["success"] is False
        assert "recipient" in result["error"].lower()

    @patch("app.services.email_service._resend_key", return_value=None)
    def test_no_api_key_noops(self, _mock_key):
        from app.services.email_service import send_email
        result = send_email(to="a@b.com", subject="Hi", html="<p>x</p>")
        assert result["success"] is False
        assert result["error"] == "Email not configured"

    @patch("app.services.email_service._resend_key", return_value="re_test_key")
    @patch("app.services.email_service.httpx.post")
    def test_successful_send(self, mock_post, _mock_key):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"id": "email_abc123"}
        mock_post.return_value = resp

        from app.services.email_service import send_email
        result = send_email(to="a@b.com", subject="Hello", html="<p>hi</p>", text="hi")

        assert result["success"] is True
        assert result["id"] == "email_abc123"
        # Verify the Resend payload shape
        kwargs = mock_post.call_args.kwargs
        assert kwargs["json"]["to"] == ["a@b.com"]
        assert kwargs["json"]["subject"] == "Hello"
        assert kwargs["headers"]["Authorization"] == "Bearer re_test_key"

    @patch("app.services.email_service._resend_key", return_value="re_test_key")
    @patch("app.services.email_service.httpx.post", side_effect=Exception("boom"))
    def test_send_failure_is_caught(self, _mock_post, _mock_key):
        from app.services.email_service import send_email
        result = send_email(to="a@b.com", subject="Hello", html="<p>hi</p>")
        assert result["success"] is False
        assert "boom" in result["error"]


# ─────────────────────────────────────────────────────────────────────
# Email fallback inside notify_booking_status_change
# ─────────────────────────────────────────────────────────────────────

class TestBookingEmailFallback:
    @patch("app.services.email_service.send_email")
    def test_email_sent_when_no_fcm_token(self, mock_send):
        mock_send.return_value = {"success": True, "id": "e1"}
        from app.utils.notifications import notify_booking_status_change

        result = notify_booking_status_change(
            status="approved",
            booking_id="b1",
            service_name="DJ",
            client_name="Priya",
            vendor_name="Raj",
            client_fcm_token=None,          # no push → email fallback
            vendor_fcm_token=None,
            client_email="priya@test.com",
            vendor_email="raj@test.com",
        )

        assert mock_send.call_count == 2
        assert result["client_email_result"]["success"] is True
        assert result["vendor_email_result"]["success"] is True
        # Subject should carry the status template title
        subjects = [c.kwargs["subject"] for c in mock_send.call_args_list]
        assert any("Approved" in s for s in subjects)

    @patch("app.utils.notifications._ensure_firebase", return_value=True)
    @patch("app.utils.notifications.send_push_notification")
    @patch("app.services.email_service.send_email")
    def test_no_email_when_push_succeeds(self, mock_send, mock_push, _fb):
        mock_push.return_value = {"success": True, "message_id": "m1"}
        from app.utils.notifications import notify_booking_status_change

        notify_booking_status_change(
            status="pending",
            booking_id="b2",
            service_name="Photo",
            client_name="A",
            vendor_name="B",
            client_fcm_token="tok_c",
            vendor_fcm_token="tok_v",
            client_email="a@test.com",
            vendor_email="b@test.com",
        )
        # Push succeeded for both → no email fallback
        mock_send.assert_not_called()

    @patch("app.services.email_service.send_email")
    def test_no_email_without_address(self, mock_send):
        from app.utils.notifications import notify_booking_status_change
        result = notify_booking_status_change(
            status="rejected",
            booking_id="b3",
            service_name="Cake",
            client_name="A",
            vendor_name="B",
            client_fcm_token=None,
            vendor_fcm_token=None,
        )
        mock_send.assert_not_called()
        assert result["client_email_result"] is None
        assert result["vendor_email_result"] is None
