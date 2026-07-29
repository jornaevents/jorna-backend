"""Business logic for user authentication (register & login)."""

import hashlib
import os
import re as _re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from sqlalchemy.orm import Session

from app.config import (
    ALGORITHM,
    SECRET_KEY,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
    PASSWORD_RESET_EXPIRE_MINUTES,
    FRONTEND_URL,
    WEB_APP_URL,
)
from app.db.models import User, RefreshToken, PasswordResetToken

SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")

# Cache the JWKS client at module level so we don't re-fetch on every request.
_jwks_client = None

def _get_jwks_client():
    from jwt import PyJWKClient
    global _jwks_client
    if _jwks_client is None and SUPABASE_URL:
        _jwks_client = PyJWKClient(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json")
    return _jwks_client

def _make_token(user_id: str, email: str, token_version: int) -> str:
    """Return a signed JWT with a fixed expiry window."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": user_id, "email": email, "exp": expire, "tv": token_version},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def _make_refresh_token(user_id: str, db: Session, family: Optional[str] = None) -> str:
    """Create, persist, and return a new opaque refresh token.

    Token format: "{family}.{secret}" — the family UUID lets us detect replay attacks
    without scanning the entire table.
    """
    if family is None:
        family = str(secrets.token_hex(16))  # 16-byte random family ID
    secret = secrets.token_urlsafe(32)
    raw_token = f"{family}.{secret}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    record = RefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        family=family,
        expires_at=now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        created_at=now,
    )
    db.add(record)
    db.flush()
    return raw_token


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


class AuthError(Exception):
    """Raised when an auth operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def refresh_access_token(*, refresh_token: str, db: Session) -> dict:
    """Validate a refresh token, rotate it, and return a new token pair.

    Reuse detection: if a family is presented but the hash doesn't match any
    live record, a previously-rotated token is being replayed — wipe all refresh
    tokens for that user immediately.
    """
    if "." not in refresh_token:
        raise AuthError(401, "Invalid refresh token")

    family, _ = refresh_token.split(".", 1)
    token_hash = _hash_token(refresh_token)
    now = datetime.now(timezone.utc)

    # Look up all live tokens in this family
    family_tokens = db.query(RefreshToken).filter(RefreshToken.family == family).all()

    if not family_tokens:
        # Family doesn't exist — could be expired/deleted or completely bogus
        raise AuthError(401, "Refresh token not found or expired")

    # Find the one that matches our hash
    matched = next((t for t in family_tokens if t.token_hash == token_hash), None)

    if matched is None:
        # Family exists but hash doesn't match → replay attack: a rotated token was reused.
        # Wipe ALL refresh tokens for this user to force re-login.
        user_id = family_tokens[0].user_id
        db.query(RefreshToken).filter(RefreshToken.user_id == user_id).delete()
        db.commit()
        raise AuthError(401, "Refresh token already used — please log in again")

    if matched.expires_at.replace(tzinfo=timezone.utc) < now:
        db.delete(matched)
        db.commit()
        raise AuthError(401, "Refresh token expired — please log in again")

    user = db.query(User).filter(User.user_id == matched.user_id).first()
    if not user:
        raise AuthError(401, "User not found")

    # Rotate: delete old token, issue new one in same family
    db.delete(matched)
    new_refresh = _make_refresh_token(user.user_id, db, family=family)
    db.commit()

    access = _make_token(user.user_id, user.email, user.token_version)
    return {"access_token": access, "refresh_token": new_refresh, "token_type": "bearer"}


def _decode_supabase_access_token(access_token: str) -> dict:
    """Verify a Supabase-issued JWT using the project's JWKS public key."""
    import logging as _log
    _logger = _log.getLogger(__name__)

    try:
        header = jwt.get_unverified_header(access_token)
    except jwt.exceptions.DecodeError as e:
        raise AuthError(400, "Invalid supabase_access_token: not a valid JWT") from e
    _logger.info("Supabase token header: %s", header)

    client = _get_jwks_client()
    if not client:
        raise AuthError(500, "Server is not configured for Google sign-in (missing SUPABASE_URL)")
    try:
        signing_key = client.get_signing_key_from_jwt(access_token)
        alg = header.get("alg", "RS256")
        _logger.info("Verifying with alg=%s", alg)
        return jwt.decode(
            access_token,
            signing_key.key,
            algorithms=[alg],
            audience="authenticated",
        )
    except jwt.InvalidTokenError as e:
        raise AuthError(401, f"Invalid Supabase token: {e}") from e


def _unique_username_from_email(email: str, db: Session) -> str:
    """Derive a unique username from the email prefix, appending a counter if needed."""
    base = _re.sub(r"[^a-zA-Z0-9_]", "", email.split("@")[0])[:20] or "user"
    username = base
    counter = 1
    while db.query(User).filter(User.username == username).first():
        username = f"{base}{counter}"
        counter += 1
    return username


def google_sign_in_or_create(*, access_token: str, db: Session) -> dict:
    """
    Verify a Supabase Google token and look up any existing Jorna account.
    Returns a JWT if an account is found. Returns is_new_user=True with the
    email if no account exists — the client must complete registration via
    /auth/register. No account is auto-created here.
    """
    claims = _decode_supabase_access_token(access_token)
    sub = (claims.get("sub") or "").lower()
    if not sub:
        raise AuthError(401, "Invalid token: missing sub")

    # Check by Supabase user ID first (Google-linked accounts)
    user = db.query(User).filter(User.supabase_user_id == sub).first()
    if user:
        meta = claims.get("user_metadata") or {}
        google_picture = meta.get("avatar_url") or meta.get("picture") or None
        if google_picture and user.pfp_url != google_picture:
            user.pfp_url = google_picture
        access = _make_token(user.user_id, user.email, user.token_version)
        refresh = _make_refresh_token(user.user_id, db)
        db.commit()
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
            "user_id": user.user_id,
            "email": user.email,
            "is_new_user": False,
        }

    email = (claims.get("email") or "").lower()
    if not email:
        raise AuthError(400, "Google account has no email address")

    meta = claims.get("user_metadata") or {}
    pfp_url = meta.get("avatar_url") or meta.get("picture") or None

    # Check by email (password-based account with same address — link it)
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        if not existing.supabase_user_id:
            existing.supabase_user_id = sub
        if pfp_url and not existing.pfp_url:
            existing.pfp_url = pfp_url
        access = _make_token(existing.user_id, existing.email, existing.token_version)
        refresh = _make_refresh_token(existing.user_id, db)
        db.commit()
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
            "user_id": existing.user_id,
            "email": existing.email,
            "is_new_user": False,
        }

    # No existing account — tell the client to complete registration
    return {
        "access_token": None,
        "token_type": "bearer",
        "user_id": None,
        "email": email,
        "is_new_user": True,
    }


def google_register(*, access_token: str, db: Session) -> dict:
    """Sign in with Google, creating the account on first use. Returns a JWT pair.

    This is what lets "Continue with Google" be the whole sign-up: everything a
    Jorna account strictly needs is either in the token (email, name, avatar) or
    derivable (a unique username from the email prefix). ``age``, ``location``,
    ``gender`` and ``language`` are nullable and get filled in later via
    ``complete_profile``; ``password`` is NULL, meaning Google-only.

    Deliberately separate from ``google_sign_in_or_create``, which still creates
    nothing. Auto-creating inside that lookup is what broke sign-up in April
    (69faa5c): clients that post a registration form *after* looking up then hit
    "email already taken". iOS still works that way, so lookup must stay pure and
    only callers that skip the form call this.

    Idempotent — a repeated call (double tap, retry after a dropped response)
    returns a session for the account it already made instead of failing.
    """
    existing = google_sign_in_or_create(access_token=access_token, db=db)
    if not existing.get("is_new_user"):
        return existing

    claims = _decode_supabase_access_token(access_token)
    sub = (claims.get("sub") or "").lower()
    email = (claims.get("email") or "").lower()
    if not sub:
        raise AuthError(401, "Invalid token: missing sub")
    if not email:
        raise AuthError(400, "Google account has no email address")

    meta = claims.get("user_metadata") or {}
    # Google sends one display name; f_name/l_name are NOT NULL, so split on the
    # first space and fall back to the email prefix when there's no name at all.
    full_name = (meta.get("full_name") or meta.get("name") or "").strip()
    first, _, last = full_name.partition(" ")
    f_name = first or email.split("@")[0]
    l_name = last.strip() or "-"

    user = User(
        email=email,
        username=_unique_username_from_email(email, db),
        password=None,  # Google-only: no password has ever been set
        f_name=f_name[:255],
        l_name=l_name[:255],
        supabase_user_id=sub,
        pfp_url=meta.get("avatar_url") or meta.get("picture") or None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    access = _make_token(user.user_id, user.email, user.token_version)
    refresh = _make_refresh_token(user.user_id, db)
    db.commit()
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "user_id": user.user_id,
        "email": user.email,
        "is_new_user": True,
    }


def register_user(
    *,
    email: str,
    password: str,
    username: str,
    phone: Optional[str] = None,
    f_name: str,
    l_name: str,
    age: int,
    location: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    gender: str,
    language: str,
    db: Session,
    supabase_user_id: Optional[str] = None,
    supabase_access_token: Optional[str] = None,
) -> dict:
    """Create a new user account. Returns ``{user_id, email}``.

    When ``supabase_user_id`` is set, ``supabase_access_token`` must prove ownership (same ``sub`` and email).
    """
    existing = db.query(User).filter(
        (User.email == email) | (User.username == username)
    ).first()
    if existing:
        raise AuthError(400, "Email or username already taken")

    if supabase_user_id:
        supabase_user_id = supabase_user_id.lower()
        if not supabase_access_token:
            raise AuthError(400, "supabase_access_token is required when linking a Google account")
        claims = _decode_supabase_access_token(supabase_access_token)
        if claims.get("sub", "").lower() != supabase_user_id:
            raise AuthError(400, "supabase_user_id does not match token")
        claim_email = (claims.get("email") or "").lower()
        if claim_email != email.lower():
            raise AuthError(400, "Email must match your Google account")
        taken = db.query(User).filter(User.supabase_user_id == supabase_user_id).first()
        if taken:
            raise AuthError(400, "This Google account is already linked to another Jorna user")
        meta = claims.get("user_metadata") or {}
        google_picture = meta.get("avatar_url") or meta.get("picture") or None
    else:
        google_picture = None

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    user = User(
        email=email,
        username=username,
        phone=phone,
        password=hashed,
        f_name=f_name,
        l_name=l_name,
        age=age,
        location=location,
        latitude=latitude,
        longitude=longitude,
        gender=gender,
        language=language,
        supabase_user_id=supabase_user_id,
        pfp_url=google_picture,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"user_id": user.user_id, "email": user.email}


def complete_profile(*, user_id: str, updates: dict, db: Session) -> dict:
    """Fill in profile fields that were left blank after Google sign-up."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise AuthError(404, "User not found")
    for field in (
        "f_name", "l_name", "age", "location", "latitude", "longitude",
        "gender", "language", "phone",
    ):
        val = updates.get(field)
        if val is not None:
            setattr(user, field, val)
    db.commit()
    profile_done = all([user.age is not None, user.location, user.gender, user.language])
    return {"message": "Profile updated", "profile_complete": profile_done}


def change_password(*, user_id: str, current_password: str, new_password: str, db: Session) -> dict:
    """Verify the current password then store a new bcrypt hash."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise AuthError(404, "User not found")
    # A Google-only account has no current password to verify against, so this is
    # the wrong door: point at the reset flow, which sets one from scratch.
    if not user.password:
        raise AuthError(
            400,
            "This account signs in with Google and has no password yet. "
            "Use the password reset to set one.",
        )
    if not bcrypt.checkpw(current_password.encode(), user.password.encode()):
        raise AuthError(401, "Current password is incorrect")
    user.password = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    return {"message": "Password updated successfully"}


def _send_password_reset_email(user: User, raw_token: str, client: str = "ios") -> None:
    """Email the user a single-use password reset link. Best-effort.

    ``client`` decides where the link lands. The default targets FRONTEND_URL,
    whose /reset-password page bounces into the iOS app via jorna://. A browser
    user passes ``web`` so the link opens the web app's own reset page (under the
    /app base path) instead of a dead-end deep link.
    """
    from app.services.email_service import send_email
    base = WEB_APP_URL if client == "web" else FRONTEND_URL
    reset_link = f"{base.rstrip('/')}/reset-password?token={raw_token}"
    subject = "Reset your Desiconnect password"
    html = (
        '<div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;'
        'padding:24px;color:#1a1a1a">'
        f'<h2 style="margin:0 0 12px">Reset your password</h2>'
        f'<p style="font-size:15px;line-height:1.5">Hi {user.f_name}, we received a request to '
        'reset your Desiconnect password. Click the button below to choose a new one.</p>'
        f'<p style="margin:24px 0"><a href="{reset_link}" '
        'style="background:#c2410c;color:#fff;text-decoration:none;padding:12px 24px;'
        'border-radius:6px;font-size:15px;display:inline-block">Reset Password</a></p>'
        f'<p style="font-size:13px;color:#555">This link expires in '
        f'{PASSWORD_RESET_EXPIRE_MINUTES} minutes. If you didn\'t request this, you can safely '
        'ignore this email — your password won\'t change.</p>'
        '<hr style="border:none;border-top:1px solid #eee;margin:20px 0">'
        '<p style="font-size:12px;color:#888;margin:0">Desiconnect — your South Asian event marketplace.</p>'
        '</div>'
    )
    text = (
        f"Hi {user.f_name},\n\nReset your Desiconnect password using this link:\n{reset_link}\n\n"
        f"This link expires in {PASSWORD_RESET_EXPIRE_MINUTES} minutes. "
        "If you didn't request this, ignore this email."
    )
    send_email(to=user.email, subject=subject, html=html, text=text)


def request_password_reset(*, email: str, db: Session, client: str = "ios") -> dict:
    """Issue a single-use reset token and email a reset link.

    Always returns the same generic response so callers can't use this endpoint
    to discover which email addresses are registered (user enumeration).
    """
    generic = {"message": "If that email is registered, a password reset link has been sent."}
    user = db.query(User).filter(User.email == email.lower().strip()).first()
    if not user:
        return generic

    # Only the most recent link should be valid — drop any prior tokens.
    db.query(PasswordResetToken).filter(PasswordResetToken.user_id == user.user_id).delete()

    raw_token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    db.add(PasswordResetToken(
        user_id=user.user_id,
        token_hash=_hash_token(raw_token),
        expires_at=now + timedelta(minutes=PASSWORD_RESET_EXPIRE_MINUTES),
        created_at=now,
    ))
    db.commit()

    try:
        _send_password_reset_email(user, raw_token, client)
    except Exception as exc:  # pragma: no cover - email is best-effort
        import logging
        logging.getLogger(__name__).error("Failed to send reset email: %s", exc)

    return generic


def reset_password(*, token: str, new_password: str, db: Session) -> dict:
    """Verify a reset token, set the new password, and invalidate all sessions."""
    record = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token_hash == _hash_token(token))
        .first()
    )
    if not record:
        raise AuthError(400, "Invalid or expired reset token")

    if record.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        db.delete(record)
        db.commit()
        raise AuthError(400, "Reset token has expired — please request a new one")

    user = db.query(User).filter(User.user_id == record.user_id).first()
    if not user:
        db.delete(record)
        db.commit()
        raise AuthError(400, "Invalid or expired reset token")

    user.password = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    # Invalidate every outstanding access token and refresh token for this user.
    user.token_version = (user.token_version or 0) + 1
    db.query(RefreshToken).filter(RefreshToken.user_id == user.user_id).delete()
    # Single-use: consume this and any sibling reset tokens.
    db.query(PasswordResetToken).filter(PasswordResetToken.user_id == user.user_id).delete()
    db.commit()
    return {"message": "Password has been reset. Please log in with your new password."}


def cleanup_expired_tokens(db: Session) -> dict:
    """Delete expired refresh and password-reset tokens.

    Both tables only ever accumulate — tokens are otherwise removed on use or
    rotation, so expired rows linger forever without a sweep. Safe to run
    repeatedly. Compares against a naive UTC now because the columns are stored
    timezone-naive (DateTime without tz). Returns the per-table delete counts.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    refresh_deleted = (
        db.query(RefreshToken).filter(RefreshToken.expires_at < now).delete(synchronize_session=False)
    )
    reset_deleted = (
        db.query(PasswordResetToken).filter(PasswordResetToken.expires_at < now).delete(synchronize_session=False)
    )
    db.commit()
    return {
        "refresh_tokens_deleted": refresh_deleted,
        "password_reset_tokens_deleted": reset_deleted,
    }


def logout_user(*, user_id: str, db: Session, refresh_token: Optional[str] = None) -> dict:
    """Invalidate all access tokens and optionally a specific refresh token.

    Bumping token_version kills all outstanding access tokens immediately.
    If refresh_token is supplied, only that device's refresh token is removed;
    otherwise all refresh tokens for the user are wiped.
    """
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise AuthError(404, "User not found")
    user.token_version = (user.token_version or 0) + 1
    if refresh_token:
        token_hash = _hash_token(refresh_token)
        db.query(RefreshToken).filter(
            RefreshToken.user_id == user_id,
            RefreshToken.token_hash == token_hash,
        ).delete()
    else:
        db.query(RefreshToken).filter(RefreshToken.user_id == user_id).delete()
    db.commit()
    return {"message": "Logged out successfully"}


def login_user(*, identifier: str, password: str, db: Session) -> dict:
    """Verify credentials and return an access + refresh token pair.

    ``identifier`` may be either an email address or a username.
    """
    import re as _re
    _email_re = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    if _email_re.match(identifier):
        user = db.query(User).filter(User.email == identifier.lower()).first()
    else:
        user = db.query(User).filter(User.username == identifier).first()
    # `user.password` is NULL for Google-only accounts — check before calling
    # .encode() on it, or a password attempt against one 500s instead of 401ing.
    # The message stays generic for every failure (unknown address, wrong
    # password, Google-only account) so it can't be used to test whether an
    # email is registered; the Google hint is safe because everyone sees it.
    if not user or not user.password or not bcrypt.checkpw(password.encode(), user.password.encode()):
        raise AuthError(401, "Invalid credentials. If you signed up with Google, use Continue with Google.")

    access = _make_token(user.user_id, user.email, user.token_version)
    refresh = _make_refresh_token(user.user_id, db)
    db.commit()
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer"}
