"""Tests for the password reset flow (forgot-password / reset-password)."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import bcrypt
import pytest

from app.db.models import User, RefreshToken, PasswordResetToken
from app.services.auth_service import (
    AuthError,
    request_password_reset,
    reset_password,
    cleanup_expired_tokens,
)
from tests.test_api import TestingSessionLocal, client, make_auth_headers


def _make_user(db, password="OldPass123"):
    uid = str(uuid.uuid4())[:8]
    user = User(
        email=f"reset_{uid}@test.com", username=f"reset_{uid}",
        password=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
        phone="1", f_name="Reset", l_name="User",
        age=25, location="NJ", gender="F", language="EN", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _request_and_capture_token(email, db):
    """Call request_password_reset, capturing the raw token from the email step."""
    captured = {}

    def _capture(user, raw_token, client="ios"):
        captured["token"] = raw_token

    with patch("app.services.auth_service._send_password_reset_email", side_effect=_capture):
        result = request_password_reset(email=email, db=db)
    return result, captured.get("token")


@pytest.fixture
def db():
    """A DB session that is always closed, even if the test fails — otherwise the
    leaked connection keeps the SQLite test file locked on Windows at teardown."""
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


class TestResetLinkTarget:
    """The reset email must point where the requesting client can act on it: iOS
    to the deep-link bridge, web to the web app's own reset page."""

    def _link_for(self, db, client_kind):
        from app.config import FRONTEND_URL, WEB_APP_URL

        user = _make_user(db)
        captured = {}

        def _capture_email(**kwargs):
            captured["body"] = f"{kwargs.get('html', '')} {kwargs.get('text', '')}"

        with patch("app.services.email_service.send_email", side_effect=_capture_email):
            request_password_reset(email=user.email, db=db, client=client_kind)
        return captured.get("body", ""), FRONTEND_URL, WEB_APP_URL

    def test_web_client_links_into_the_web_app(self, db):
        body, frontend, web = self._link_for(db, "web")
        assert f"{web.rstrip('/')}/reset-password?token=" in body
        assert f"{frontend.rstrip('/')}/reset-password" not in body

    def test_default_client_links_to_the_frontend_bridge(self, db):
        body, frontend, web = self._link_for(db, "ios")
        assert f"{frontend.rstrip('/')}/reset-password?token=" in body


class TestRequestReset:
    def test_unknown_email_is_generic_and_creates_no_token(self, db):
        result, token = _request_and_capture_token("nobody@nowhere.com", db)
        assert "if that email is registered" in result["message"].lower()
        assert token is None

    def test_known_email_creates_token_and_sends_email(self, db):
        user = _make_user(db)
        result, token = _request_and_capture_token(user.email, db)
        assert token is not None
        # A hashed token row exists for the user
        rows = db.query(PasswordResetToken).filter(PasswordResetToken.user_id == user.user_id).all()
        assert len(rows) == 1

    def test_new_request_supersedes_old_token(self, db):
        user = _make_user(db)
        _, token1 = _request_and_capture_token(user.email, db)
        _, token2 = _request_and_capture_token(user.email, db)
        assert token1 != token2
        # Only the newest token survives
        rows = db.query(PasswordResetToken).filter(PasswordResetToken.user_id == user.user_id).all()
        assert len(rows) == 1
        # The old token no longer works
        with pytest.raises(AuthError):
            reset_password(token=token1, new_password="BrandNew123", db=db)


class TestResetPassword:
    def test_valid_token_changes_password(self, db):
        user = _make_user(db, password="OldPass123")
        _, token = _request_and_capture_token(user.email, db)

        reset_password(token=token, new_password="BrandNew123", db=db)

        db.refresh(user)
        assert bcrypt.checkpw(b"BrandNew123", user.password.encode())
        assert not bcrypt.checkpw(b"OldPass123", user.password.encode())

    def test_reset_bumps_token_version_and_wipes_refresh_tokens(self, db):
        user = _make_user(db)
        db.add(RefreshToken(
            user_id=user.user_id, token_hash="x" * 64, family="fam",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()
        old_version = user.token_version

        _, token = _request_and_capture_token(user.email, db)
        reset_password(token=token, new_password="BrandNew123", db=db)

        db.refresh(user)
        assert user.token_version == old_version + 1
        assert db.query(RefreshToken).filter(RefreshToken.user_id == user.user_id).count() == 0

    def test_token_is_single_use(self, db):
        user = _make_user(db)
        _, token = _request_and_capture_token(user.email, db)
        reset_password(token=token, new_password="BrandNew123", db=db)
        # Re-using the same token fails
        with pytest.raises(AuthError) as exc:
            reset_password(token=token, new_password="Another123", db=db)
        assert exc.value.status_code == 400

    def test_invalid_token_rejected(self, db):
        with pytest.raises(AuthError) as exc:
            reset_password(token="not-a-real-token", new_password="BrandNew123", db=db)
        assert exc.value.status_code == 400

    def test_expired_token_rejected(self, db):
        user = _make_user(db)
        _, token = _request_and_capture_token(user.email, db)
        # Force-expire the token
        from app.services.auth_service import _hash_token
        row = db.query(PasswordResetToken).filter(
            PasswordResetToken.token_hash == _hash_token(token)
        ).first()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

        with pytest.raises(AuthError) as exc:
            reset_password(token=token, new_password="BrandNew123", db=db)
        assert exc.value.status_code == 400


class TestResetEndpoints:
    def test_forgot_password_always_200(self):
        # Unknown email still returns 200 (no enumeration)
        resp = client.post("/auth/forgot-password", json={"email": "ghost@nowhere.com"})
        assert resp.status_code == 200
        assert "message" in resp.json()

    def test_reset_password_weak_password_422(self):
        resp = client.post("/auth/reset-password", json={"token": "abc", "new_password": "weak"})
        assert resp.status_code == 422

    def test_reset_password_invalid_token_400(self):
        resp = client.post(
            "/auth/reset-password",
            json={"token": "bogus", "new_password": "StrongPass123"},
        )
        assert resp.status_code == 400


class TestExpiredTokenCleanup:
    def test_cleanup_deletes_only_expired(self, db):
        user = _make_user(db)
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        # One expired + one live refresh token
        db.add(RefreshToken(
            user_id=user.user_id, token_hash="exp" + "a" * 61, family="f1",
            expires_at=now - timedelta(days=1), created_at=now - timedelta(days=31),
        ))
        db.add(RefreshToken(
            user_id=user.user_id, token_hash="live" + "b" * 60, family="f2",
            expires_at=now + timedelta(days=1), created_at=now,
        ))
        # One expired reset token
        db.add(PasswordResetToken(
            user_id=user.user_id, token_hash="exp" + "c" * 61,
            expires_at=now - timedelta(minutes=5), created_at=now - timedelta(hours=2),
        ))
        db.commit()

        result = cleanup_expired_tokens(db)

        assert result["refresh_tokens_deleted"] == 1
        assert result["password_reset_tokens_deleted"] == 1
        # The live refresh token survives
        live = db.query(RefreshToken).filter(RefreshToken.user_id == user.user_id).all()
        assert len(live) == 1
        assert live[0].family == "f2"
