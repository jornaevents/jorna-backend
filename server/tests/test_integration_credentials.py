"""
Integration tests that use REAL Firebase and Google OAuth credentials.

These tests verify that the actual credential files are valid and that
the SDKs initialise correctly — no mocking.  They require:
  • server/firebase_credentials.json
  • server/client_secret.json

Tests that would charge money or require interactive browser auth
(e.g. actually sending a notification, completing Google OAuth consent)
are designed to hit the SDK initialisation and validation layer only,
not the live network send path.

Run:
    cd server
    source venv/bin/activate
    python -m pytest tests/test_integration_credentials.py -v
"""

import json
import os
import pytest
from unittest.mock import patch

from tests.test_api import TestingSessionLocal, client, make_auth_headers
from app.db.models import User, Vendor, Service, Booking

# ---------------------------------------------------------------------------
# Skip the entire module when credential files are missing
# ---------------------------------------------------------------------------
FIREBASE_CREDS = os.environ.get("FIREBASE_CREDENTIALS_PATH", "firebase_credentials.json")
GOOGLE_CREDS = os.environ.get("GOOGLE_CLIENT_SECRETS_FILE", "client_secret.json")

firebase_available = os.path.exists(FIREBASE_CREDS)
google_available = os.path.exists(GOOGLE_CREDS)


# ═══════════════════════════════════════════════════════════════════════
# 1. FIREBASE CREDENTIAL TESTS (no mocks)
# ═══════════════════════════════════════════════════════════════════════

class TestFirebaseCredentials:
    """Validate that the real firebase_credentials.json is correctly
    structured and the Admin SDK can initialise from it."""

    @pytest.mark.skipif(not firebase_available, reason="firebase_credentials.json not found")
    def test_firebase_credentials_file_is_valid_json(self):
        """The credentials file must be parseable JSON."""
        with open(FIREBASE_CREDS) as f:
            data = json.load(f)
        assert isinstance(data, dict)

    @pytest.mark.skipif(not firebase_available, reason="firebase_credentials.json not found")
    def test_firebase_credentials_has_required_fields(self):
        """Firebase service-account JSON must contain critical fields."""
        with open(FIREBASE_CREDS) as f:
            data = json.load(f)
        required = ["type", "project_id", "private_key_id", "private_key", "client_email"]
        for field in required:
            assert field in data, f"Missing required field: {field}"
        assert data["type"] == "service_account"

    @pytest.mark.skipif(not firebase_available, reason="firebase_credentials.json not found")
    def test_firebase_admin_sdk_initialises(self):
        """The real credentials should allow firebase_admin to initialise."""
        import firebase_admin
        from firebase_admin import credentials as fb_creds

        # Delete any existing app from previous test runs / module-level init
        try:
            existing = firebase_admin.get_app()
            firebase_admin.delete_app(existing)
        except ValueError:
            pass  # no app exists yet

        cred = fb_creds.Certificate(FIREBASE_CREDS)
        app = firebase_admin.initialize_app(cred, name="integration_test")
        try:
            assert app is not None
            assert app.name == "integration_test"
            assert app.project_id is not None
        finally:
            firebase_admin.delete_app(app)

    @pytest.mark.skipif(not firebase_available, reason="firebase_credentials.json not found")
    def test_ensure_firebase_returns_true(self):
        """The _ensure_firebase helper must succeed with real creds."""
        # Reset the singleton so it re-initialises
        import app.utils.notifications as notif_module
        notif_module._firebase_app = None

        result = notif_module._ensure_firebase()
        assert result is True, "_ensure_firebase() should return True with valid credentials"

    @pytest.mark.skipif(not firebase_available, reason="firebase_credentials.json not found")
    def test_send_notification_to_invalid_token_returns_error(self):
        """Sending to a fake device token should connect to Firebase but
        return an error — proving the SDK is fully operational."""
        import app.utils.notifications as notif_module
        notif_module._firebase_app = None  # force re-init

        result = notif_module.send_push_notification(
            fcm_token="fake_device_token_that_does_not_exist",
            title="Test",
            body="Integration test — should fail gracefully",
        )
        # Firebase WILL try to send and fail with InvalidArgument or Unregistered
        assert isinstance(result, dict)
        assert "success" in result
        # We expect failure because the token is invalid, but the SDK was operational
        if not result["success"]:
            assert "error" in result
            # The error should be from Firebase, NOT "Firebase not configured"
            assert result["error"] != "Firebase not configured", (
                "Firebase should have initialised with real creds"
            )

    @pytest.mark.skipif(not firebase_available, reason="firebase_credentials.json not found")
    def test_full_notification_dispatch_with_invalid_tokens(self):
        """notify_booking_status_change with fake tokens: Firebase is live,
        but the tokens don't correspond to real devices."""
        import app.utils.notifications as notif_module
        notif_module._firebase_app = None

        result = notif_module.notify_booking_status_change(
            status="pending",
            booking_id="integration-test-booking",
            service_name="Test DJ",
            client_name="Test Client",
            vendor_name="Test Vendor",
            client_fcm_token="fake_client_token_abc",
            vendor_fcm_token="fake_vendor_token_xyz",
        )
        assert "client_result" in result
        assert "vendor_result" in result
        # Both should have attempted to send (not skipped)
        for key in ("client_result", "vendor_result"):
            assert result[key]["success"] is False
            assert result[key]["error"] != "Firebase not configured"

    @pytest.mark.skipif(not firebase_available, reason="firebase_credentials.json not found")
    def test_checkin_notification_with_invalid_token(self):
        """notify_check_in with a fake token — proves Firebase is live."""
        import app.utils.notifications as notif_module
        notif_module._firebase_app = None

        result = notif_module.notify_check_in(
            booking_id="integration-checkin",
            is_vendor=True,
            client_name="Test Client",
            vendor_name="Test Vendor",
            recipient_fcm_token="fake_recipient_token_999",
        )
        assert result["success"] is False
        assert result["error"] != "Firebase not configured"

    @pytest.mark.skipif(not firebase_available, reason="firebase_credentials.json not found")
    def test_booking_creation_endpoint_dispatches_real_firebase(self):
        """POST /bookings with real Firebase — notification result should
        show Firebase attempted delivery, not 'Firebase not configured'."""
        import app.utils.notifications as notif_module
        notif_module._firebase_app = None

        db = TestingSessionLocal()
        # Create user + vendor + service
        user = User(
            email="integ_client@test.com", username="integ_client",
            password="pw", phone="1", f_name="Integ", l_name="Client",
            age=25, location="123", gender="F", language="EN",
            fcm_token="fake_integ_client_token",
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        vendor_user = User(
            email="integ_vendor@test.com", username="integ_vendor",
            password="pw", phone="1", f_name="Integ", l_name="Vendor",
            age=30, location="456", gender="M", language="HI",
            fcm_token="fake_integ_vendor_token",
        )
        db.add(vendor_user)
        db.commit()
        db.refresh(vendor_user)

        vendor = Vendor(
            user_id=vendor_user.user_id, bio="Integration DJ",
            rating=4.9, num_events=50,
        )
        db.add(vendor)
        db.commit()
        db.refresh(vendor)

        service = Service(
            name="Integration DJ Set", price=500.0,
            duration_minutes=180, vendor_id=vendor.vendor_id,
            experience="10 years",
        )
        db.add(service)
        db.commit()
        db.refresh(service)

        service_id = service.service_id
        auth_headers = make_auth_headers(user)
        db.close()

        response = client.post(
            "/bookings",
            json={
                "service_id": service_id,
                "event_name": "Integration Diwali",
                "time_start": "18:00",
                "time_end": "22:00",
                "location": "Community Hall",
                "date_iso": "2026-11-01",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "pending"
        assert "notification" in data

        notif = data["notification"]
        # With real Firebase, the errors should NOT be "Firebase not configured"
        for key in ("client_result", "vendor_result"):
            assert notif[key].get("error") != "Firebase not configured", (
                f"Expected real Firebase attempt, got: {notif[key]}"
            )


# ═══════════════════════════════════════════════════════════════════════
# 2. GOOGLE OAUTH CREDENTIAL TESTS (no mocks)
# ═══════════════════════════════════════════════════════════════════════

class TestGoogleOAuthCredentials:
    """Validate the real client_secret.json and Google OAuth flow setup."""

    @pytest.mark.skipif(not google_available, reason="client_secret.json not found")
    def test_client_secret_file_is_valid_json(self):
        """The credentials file must be parseable JSON."""
        with open(GOOGLE_CREDS) as f:
            data = json.load(f)
        assert isinstance(data, dict)

    @pytest.mark.skipif(not google_available, reason="client_secret.json not found")
    def test_client_secret_has_required_structure(self):
        """Google OAuth JSON must have a top-level 'web' or 'installed' key
        containing client_id and client_secret."""
        with open(GOOGLE_CREDS) as f:
            data = json.load(f)
        top_key = next(iter(data))
        assert top_key in ("web", "installed"), f"Unexpected top-level key: {top_key}"
        inner = data[top_key]
        assert "client_id" in inner, "Missing client_id"
        assert "client_secret" in inner, "Missing client_secret"
        assert "redirect_uris" in inner or "auth_uri" in inner, "Missing redirect/auth URIs"

    @pytest.mark.skipif(not google_available, reason="client_secret.json not found")
    def test_load_client_credentials_returns_real_values(self):
        """_load_client_credentials() must return non-None values from the real file."""
        from app.utils.calendar import _load_client_credentials
        client_id, client_secret = _load_client_credentials()
        assert client_id is not None, "client_id should not be None"
        assert client_secret is not None, "client_secret should not be None"
        assert len(client_id) > 10, "client_id looks too short to be real"
        assert len(client_secret) > 5, "client_secret looks too short to be real"

    @pytest.mark.skipif(not google_available, reason="client_secret.json not found")
    def test_google_auth_flow_initialises_with_real_creds(self):
        """get_google_auth_flow() must build a real Flow object."""
        from app.utils.calendar import get_google_auth_flow
        flow = get_google_auth_flow("http://localhost:8000/vendors/auth/callback")
        assert flow is not None
        assert hasattr(flow, "authorization_url")
        assert hasattr(flow, "fetch_token")

    @pytest.mark.skipif(not google_available, reason="client_secret.json not found")
    def test_google_auth_flow_generates_valid_auth_url(self):
        """The generated auth URL must point to Google and contain required params."""
        from app.utils.calendar import get_google_auth_flow
        flow = get_google_auth_flow("http://localhost:8000/vendors/auth/callback")
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            state="test-vendor-id",
        )
        assert "accounts.google.com" in auth_url
        assert "client_id=" in auth_url
        assert "calendar.readonly" in auth_url or "calendar" in auth_url
        assert "redirect_uri=" in auth_url
        assert state is not None

    @pytest.mark.skipif(not google_available, reason="client_secret.json not found")
    def test_google_auth_endpoint_returns_real_auth_url(self):
        """GET /vendors/{id}/google-auth should return a real Google URL
        (not a mocked one) when client_secret.json is present."""
        db = TestingSessionLocal()
        user = User(
            email="real_oauth@test.com", username="real_oauth",
            password="pw", phone="1", f_name="R", l_name="O",
            age=25, location="123", gender="M", language="EN",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        vendor = Vendor(
            user_id=user.user_id, bio="Real OAuth Vendor",
            rating=5.0, num_events=1,
        )
        db.add(vendor)
        db.commit()
        db.refresh(vendor)

        response = client.get(f"/vendors/{vendor.vendor_id}/google-auth")
        assert response.status_code == 200
        data = response.json()
        auth_url = data["auth_url"]

        # Verify it's a real Google URL with our client_id embedded
        assert "accounts.google.com" in auth_url
        assert "state=" in auth_url  # state param is HMAC-encoded, not raw vendor_id

        # Verify the client_id from our real file is in the URL
        from app.utils.calendar import _load_client_credentials
        real_client_id, _ = _load_client_credentials()
        if real_client_id:
            assert real_client_id in auth_url, (
                "Auth URL should contain the real client_id from client_secret.json"
            )

        db.close()

    @pytest.mark.skipif(not google_available, reason="client_secret.json not found")
    def test_create_calendar_service_with_real_client_creds(self):
        """create_google_calendar_service() with a fake access token but
        real client_id/secret should build a valid service object."""
        from app.utils.calendar import create_google_calendar_service
        service, creds = create_google_calendar_service(
            access_token="fake_access_token_for_test",
            refresh_token="fake_refresh_token_for_test",
        )
        assert service is not None
        assert hasattr(service, "freebusy")  # Calendar API service
        # Verify real client credentials were loaded
        assert creds.client_id is not None
        assert creds.client_secret is not None

    @pytest.mark.skipif(not google_available, reason="client_secret.json not found")
    def test_google_callback_with_invalid_code_returns_400(self):
        """Passing a bogus auth code to the real OAuth flow should return
        a 400 error, NOT a 500 — proving the flow initialises correctly."""
        db = TestingSessionLocal()
        user = User(
            email="callback_real@test.com", username="callback_real",
            password="pw", phone="1", f_name="C", l_name="R",
            age=25, location="123", gender="M", language="EN",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        vendor = Vendor(
            user_id=user.user_id, bio="Callback Test",
            rating=5.0, num_events=1,
        )
        db.add(vendor)
        db.commit()
        db.refresh(vendor)

        from app.services.calendar_service import _encode_state
        state = _encode_state(str(vendor.vendor_id), "test-code-verifier")
        response = client.get(
            f"/vendors/auth/callback?state={state}&code=totally_fake_auth_code",
            follow_redirects=False,
        )
        # Callback always redirects; bad code → success=false in location
        assert response.status_code in (302, 307)
        assert "success=false" in response.headers["location"]

        db.close()


# ═══════════════════════════════════════════════════════════════════════
# 3. TOKEN REFRESH PERSISTENCE TESTS
# ═══════════════════════════════════════════════════════════════════════

class TestTokenRefreshPersistence:
    """Verify that when Google auto-refreshes an access token, the new
    token is persisted back to the Vendor table."""

    @pytest.mark.skipif(not google_available, reason="client_secret.json not found")
    def test_refreshed_token_is_persisted_to_db(self):
        """Simulate a token refresh by creating a service where the
        credentials object ends up with a different token after the API call."""
        from unittest.mock import MagicMock, PropertyMock

        db = TestingSessionLocal()
        user = User(
            email="refresh_persist@test.com", username="refresh_persist",
            password="pw", phone="1", f_name="R", l_name="P",
            age=25, location="123", gender="M", language="EN",
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        vendor = Vendor(
            user_id=user.user_id, bio="Refresh Test",
            rating=5.0, num_events=1,
            google_access_token="old_token_before_refresh",
            google_refresh_token="real_refresh_token",
        )
        db.add(vendor)
        db.commit()
        db.refresh(vendor)

        # Mock the calendar service creation to return creds with a
        # DIFFERENT token (simulating auto-refresh)
        mock_creds = MagicMock()
        mock_creds.token = "new_refreshed_token_xyz"  # Different from "old_token_before_refresh"
        mock_service = MagicMock()

        mock_freebusy = MagicMock()
        mock_freebusy.return_value = []  # no busy blocks

        with patch("app.services.calendar_service.create_google_calendar_service",
                   return_value=(mock_service, mock_creds)), \
             patch("app.services.calendar_service.get_freebusy_schedule",
                   return_value=[]):

            response = client.get(
                f"/vendors/{vendor.vendor_id}/availability"
                f"?start_date=2026-03-01T00:00:00Z&end_date=2026-03-07T23:59:59Z"
            )

        assert response.status_code == 200

        # Verify the NEW token was persisted to DB
        db.refresh(vendor)
        assert vendor.google_access_token == "new_refreshed_token_xyz", (
            f"Expected 'new_refreshed_token_xyz', got '{vendor.google_access_token}'"
        )
        db.close()

    @pytest.mark.skipif(not google_available, reason="client_secret.json not found")
    def test_unchanged_token_is_not_rewritten(self):
        """If the token didn't change (no refresh happened), the DB
        should NOT be updated unnecessarily."""
        from unittest.mock import MagicMock

        db = TestingSessionLocal()
        user = User(
            email="no_refresh@test.com", username="no_refresh",
            password="pw", phone="1", f_name="N", l_name="R",
            age=25, location="123", gender="M", language="EN",
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        vendor = Vendor(
            user_id=user.user_id, bio="No Refresh",
            rating=5.0, num_events=1,
            google_access_token="same_token",
            google_refresh_token="refresh_token",
        )
        db.add(vendor)
        db.commit()
        db.refresh(vendor)

        mock_creds = MagicMock()
        mock_creds.token = "same_token"  # Same as stored — no refresh happened

        with patch("app.services.calendar_service.create_google_calendar_service",
                   return_value=(MagicMock(), mock_creds)), \
             patch("app.services.calendar_service.get_freebusy_schedule",
                   return_value=[]):

            response = client.get(
                f"/vendors/{vendor.vendor_id}/availability"
                f"?start_date=2026-03-01T00:00:00Z&end_date=2026-03-07T23:59:59Z"
            )

        assert response.status_code == 200
        db.refresh(vendor)
        assert vendor.google_access_token == "same_token"
        db.close()


# ═══════════════════════════════════════════════════════════════════════
# 4. CROSS-CUTTING: env var loading
# ═══════════════════════════════════════════════════════════════════════

class TestEnvConfiguration:
    """Verify that environment variables are loaded correctly."""

    def test_dotenv_loads_firebase_path(self):
        """FIREBASE_CREDENTIALS_PATH should be set (from .env or default)."""
        path = os.environ.get("FIREBASE_CREDENTIALS_PATH", "firebase_credentials.json")
        assert path is not None
        assert path.endswith(".json")

    def test_dotenv_loads_google_path(self):
        """GOOGLE_CLIENT_SECRETS_FILE should be set (from .env or default)."""
        path = os.environ.get("GOOGLE_CLIENT_SECRETS_FILE", "client_secret.json")
        assert path is not None
        assert path.endswith(".json")

    def test_firebase_credentials_file_exists(self):
        """The file referenced by FIREBASE_CREDENTIALS_PATH must exist."""
        path = os.environ.get("FIREBASE_CREDENTIALS_PATH", "firebase_credentials.json")
        assert os.path.exists(path), (
            f"Firebase credentials file not found at '{path}'. "
            "Download from Firebase Console → Project Settings → Service Accounts."
        )

    def test_google_client_secret_file_exists(self):
        """The file referenced by GOOGLE_CLIENT_SECRETS_FILE must exist."""
        path = os.environ.get("GOOGLE_CLIENT_SECRETS_FILE", "client_secret.json")
        assert os.path.exists(path), (
            f"Google client secret file not found at '{path}'. "
            "Download from Google Cloud Console → APIs & Services → Credentials."
        )
