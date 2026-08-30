"""Tests for the email-verification flow: register issues a token, Google
sign-up skips it entirely, /auth/verify-email consumes it, resend reissues
it, and the money/messaging endpoints refuse an unverified caller."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import bcrypt
import pytest

from app.db.models import User, Vendor, Service, EmailVerificationToken
from app.services.auth_service import (
    AuthError,
    register_user,
    google_register,
    resend_verification_email,
    verify_email,
    cleanup_expired_tokens,
)
from app.limiter import limiter
from tests.test_api import TestingSessionLocal, client, make_auth_headers


@pytest.fixture(autouse=True)
def without_rate_limits():
    """Several tests here call /auth/register or /auth/resend-verification more
    than their per-minute quota, all from one client address in the same run."""
    limiter.enabled = False
    yield
    limiter.enabled = True


@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def _register_kwargs(email, username, **overrides):
    kwargs = dict(
        email=email, password="StrongPass123", username=username,
        f_name="Priya", l_name="Patel", age=25, location="NJ",
        gender="F", language="EN",
    )
    kwargs.update(overrides)
    return kwargs


def _register_and_capture_token(db, email, username, **overrides):
    """Call register_user, capturing the raw verification token."""
    captured = {}

    def _capture(user, raw_token, client="ios"):
        captured["token"] = raw_token

    with patch("app.services.auth_service._send_verification_email", side_effect=_capture):
        result = register_user(db=db, **_register_kwargs(email, username, **overrides))
    return result, captured.get("token")


class TestRegisterStartsUnverified:
    def test_password_registration_is_unverified_and_sends_a_token(self, db):
        uid = uuid.uuid4().hex[:8]
        result, token = _register_and_capture_token(db, f"pw_{uid}@test.com", f"pw_{uid}")
        assert token is not None

        user = db.query(User).filter(User.user_id == result["user_id"]).first()
        assert user.email_verified is False
        assert user.email_verification_sent_at is not None

        rows = db.query(EmailVerificationToken).filter(EmailVerificationToken.user_id == user.user_id).all()
        assert len(rows) == 1

    def test_a_failed_email_send_does_not_fail_registration(self, db):
        """Best-effort, like password reset: register still succeeds even if
        the email provider is down."""
        uid = uuid.uuid4().hex[:8]
        with patch("app.services.auth_service._send_verification_email", side_effect=RuntimeError("boom")):
            result = register_user(db=db, **_register_kwargs(f"fail_{uid}@test.com", f"fail_{uid}"))
        user = db.query(User).filter(User.user_id == result["user_id"]).first()
        assert user.email_verified is False


class TestGoogleAccountsSkipVerification:
    def test_google_register_is_verified_immediately(self, db):
        from app.services import auth_service

        uid = uuid.uuid4().hex[:8]
        claims = {"sub": str(uuid.uuid4()), "email": f"g_{uid}@test.com", "user_metadata": {"full_name": "Asha Mehta"}}
        with patch.object(auth_service, "_decode_supabase_access_token", return_value=claims):
            result = google_register(access_token="fake", db=db)

        user = db.query(User).filter(User.user_id == result["user_id"]).first()
        assert user.email_verified is True
        # No token was issued — nothing to verify.
        assert db.query(EmailVerificationToken).filter(EmailVerificationToken.user_id == user.user_id).count() == 0

    def test_registration_linked_to_a_verified_google_account_skips_it_too(self, db):
        from app.services import auth_service

        uid = uuid.uuid4().hex[:8]
        sub = str(uuid.uuid4())
        email = f"linked_{uid}@test.com"
        claims = {"sub": sub, "email": email, "user_metadata": {}}
        with patch.object(auth_service, "_decode_supabase_access_token", return_value=claims):
            result = register_user(
                db=db,
                **_register_kwargs(email, f"linked_{uid}", supabase_user_id=sub, supabase_access_token="fake"),
            )
        user = db.query(User).filter(User.user_id == result["user_id"]).first()
        assert user.email_verified is True


class TestVerifyEmail:
    def test_valid_token_verifies_the_account(self, db):
        uid = uuid.uuid4().hex[:8]
        _, token = _register_and_capture_token(db, f"v_{uid}@test.com", f"v_{uid}")

        result = verify_email(token=token, db=db)
        assert result["email"] == f"v_{uid}@test.com"

        user = db.query(User).filter(User.email == f"v_{uid}@test.com").first()
        assert user.email_verified is True

    def test_token_is_single_use(self, db):
        uid = uuid.uuid4().hex[:8]
        _, token = _register_and_capture_token(db, f"single_{uid}@test.com", f"single_{uid}")
        verify_email(token=token, db=db)
        with pytest.raises(AuthError) as exc:
            verify_email(token=token, db=db)
        assert exc.value.status_code == 400

    def test_invalid_token_rejected(self, db):
        with pytest.raises(AuthError) as exc:
            verify_email(token="not-a-real-token", db=db)
        assert exc.value.status_code == 400

    def test_expired_token_rejected(self, db):
        uid = uuid.uuid4().hex[:8]
        _, token = _register_and_capture_token(db, f"exp_{uid}@test.com", f"exp_{uid}")
        from app.services.auth_service import _hash_token
        row = db.query(EmailVerificationToken).filter(EmailVerificationToken.token_hash == _hash_token(token)).first()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

        with pytest.raises(AuthError) as exc:
            verify_email(token=token, db=db)
        assert exc.value.status_code == 400


class TestResendVerification:
    def test_resend_issues_a_new_token_and_supersedes_the_old_one(self, db):
        uid = uuid.uuid4().hex[:8]
        result, token1 = _register_and_capture_token(db, f"re_{uid}@test.com", f"re_{uid}")

        captured = {}

        def _capture(user, raw_token, client="ios"):
            captured["token"] = raw_token

        with patch("app.services.auth_service._send_verification_email", side_effect=_capture):
            resend_verification_email(user_id=result["user_id"], db=db)
        token2 = captured["token"]

        assert token1 != token2
        rows = db.query(EmailVerificationToken).filter(EmailVerificationToken.user_id == result["user_id"]).all()
        assert len(rows) == 1
        with pytest.raises(AuthError):
            verify_email(token=token1, db=db)
        # The new one still works.
        verify_email(token=token2, db=db)

    def test_resend_on_an_already_verified_account_is_a_no_op(self, db):
        uid = uuid.uuid4().hex[:8]
        result, token = _register_and_capture_token(db, f"done_{uid}@test.com", f"done_{uid}")
        verify_email(token=token, db=db)

        out = resend_verification_email(user_id=result["user_id"], db=db)
        assert out["already_verified"] is True
        assert db.query(EmailVerificationToken).filter(EmailVerificationToken.user_id == result["user_id"]).count() == 0


class TestCleanup:
    def test_cleanup_sweeps_expired_verification_tokens(self, db):
        uid = uuid.uuid4().hex[:8]
        _, token = _register_and_capture_token(db, f"cleanup_{uid}@test.com", f"cleanup_{uid}")
        from app.services.auth_service import _hash_token
        row = db.query(EmailVerificationToken).filter(EmailVerificationToken.token_hash == _hash_token(token)).first()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

        result = cleanup_expired_tokens(db)
        assert result["email_verification_tokens_deleted"] == 1


class TestVerifyEmailEndpoint:
    def test_valid_token_shows_a_success_page(self, db):
        uid = uuid.uuid4().hex[:8]
        _, token = _register_and_capture_token(db, f"ep_{uid}@test.com", f"ep_{uid}")

        res = client.get(f"/auth/verify-email?token={token}")
        assert res.status_code == 200
        assert "verified" in res.text.lower()

    def test_invalid_token_shows_a_failure_page(self):
        res = client.get("/auth/verify-email?token=bogus")
        assert res.status_code == 400
        assert "didn't work" in res.text.lower()


class TestResendVerificationEndpoint:
    def _make_unverified_user(self, db):
        uid = uuid.uuid4().hex[:8]
        user = User(
            email=f"resend_ep_{uid}@test.com", username=f"resend_ep_{uid}",
            password=bcrypt.hashpw(b"Pass1234", bcrypt.gensalt()).decode(),
            f_name="R", l_name="U", age=25, location="NJ", gender="F", language="EN",
            token_version=0, email_verified=False,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    def test_resend_endpoint_sends_a_new_link(self, db):
        user = self._make_unverified_user(db)
        with patch("app.services.auth_service._send_verification_email"):
            res = client.post("/auth/resend-verification", headers=make_auth_headers(user))
        assert res.status_code == 200, res.text
        assert res.json()["already_verified"] is False


class TestGatingOnMoneyAndMessaging:
    """A representative sample of the endpoints wired to get_current_verified_user
    — the dependency itself is unit-testable once; these confirm the swap
    actually landed on real routes."""

    @pytest.fixture
    def unverified_user_and_service(self, db):
        uid = uuid.uuid4().hex[:8]
        user = User(
            email=f"gate_{uid}@test.com", username=f"gate_{uid}",
            password="pw", f_name="G", l_name="U", age=25, location="NJ",
            gender="F", language="EN", token_version=0, email_verified=False,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        vendor_user = User(
            email=f"gate_vendor_{uid}@test.com", username=f"gate_vendor_{uid}",
            password="pw", f_name="V", l_name="U", age=30, location="NJ",
            gender="M", language="EN", token_version=0, email_verified=True,
        )
        db.add(vendor_user)
        db.commit()
        db.refresh(vendor_user)

        vendor = Vendor(user_id=vendor_user.user_id, bio="bio", rating=5.0, num_events=1)
        db.add(vendor)
        db.commit()
        db.refresh(vendor)

        service = Service(name="Test Service", price=100.0, duration_minutes=60, vendor_id=vendor.vendor_id, experience="none")
        db.add(service)
        db.commit()
        db.refresh(service)

        return user, service

    def test_unverified_user_cannot_create_a_booking(self, unverified_user_and_service):
        user, service = unverified_user_and_service
        res = client.post(
            "/bookings",
            json={
                "service_id": service.service_id,
                "event_name": "Test Event",
                "time_start": "13:00",
                "time_end": "15:00",
                "location": "123 Test St",
                "date_iso": "2026-05-01",
            },
            headers=make_auth_headers(user),
        )
        assert res.status_code == 403
        assert "verify your email" in res.json()["detail"].lower()

    def test_unverified_user_cannot_send_an_enquiry(self, unverified_user_and_service):
        user, service = unverified_user_and_service
        res = client.post(
            "/conversations/enquiry",
            json={"vendor_id": service.vendor_id, "content": "Hi, are you available?"},
            headers=make_auth_headers(user),
        )
        assert res.status_code == 403

    def test_once_verified_the_same_booking_succeeds(self, db, unverified_user_and_service):
        user, service = unverified_user_and_service
        user.email_verified = True
        db.commit()

        res = client.post(
            "/bookings",
            json={
                "service_id": service.service_id,
                "event_name": "Test Event",
                "time_start": "13:00",
                "time_end": "15:00",
                "location": "123 Test St",
                "date_iso": "2026-05-01",
            },
            headers=make_auth_headers(user),
        )
        assert res.status_code == 200, res.text

    def test_unverified_user_can_still_browse(self, unverified_user_and_service):
        """Log in, view your own profile, search — none of that is gated."""
        user, _ = unverified_user_and_service
        res = client.get("/me", headers=make_auth_headers(user))
        assert res.status_code == 200
