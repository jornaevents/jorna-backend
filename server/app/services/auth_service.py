"""Business logic for user authentication (register & login)."""

import os
import re as _re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from sqlalchemy.orm import Session

from app.config import ALGORITHM, SECRET_KEY, ACCESS_TOKEN_EXPIRE_MINUTES
from app.db.models import User

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


class AuthError(Exception):
    """Raised when an auth operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


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
            db.commit()
        token = _make_token(user.user_id, user.email, user.token_version)
        return {
            "access_token": token,
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
        db.commit()
        token = _make_token(existing.user_id, existing.email, existing.token_version)
        return {
            "access_token": token,
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
    for field in ("f_name", "l_name", "age", "location", "gender", "language", "phone"):
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
    if not bcrypt.checkpw(current_password.encode(), user.password.encode()):
        raise AuthError(401, "Current password is incorrect")
    user.password = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    return {"message": "Password updated successfully"}


def logout_user(*, user_id: str, db: Session) -> dict:
    """Invalidate all tokens for this user by bumping token_version."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise AuthError(404, "User not found")
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    return {"message": "Logged out successfully"}


def login_user(*, identifier: str, password: str, db: Session) -> dict:
    """Verify credentials and return a JWT.  Returns ``{access_token, token_type}``.

    ``identifier`` may be either an email address or a username.
    """
    import re as _re
    _email_re = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    if _email_re.match(identifier):
        user = db.query(User).filter(User.email == identifier.lower()).first()
    else:
        user = db.query(User).filter(User.username == identifier).first()
    if not user or not bcrypt.checkpw(password.encode(), user.password.encode()):
        raise AuthError(401, "Invalid credentials")

    token = _make_token(user.user_id, user.email, user.token_version)
    return {"access_token": token, "token_type": "bearer"}
