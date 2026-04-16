"""Business logic for user authentication (register & login)."""

import os
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

def _make_token(user_id: str, email: str) -> str:
    """Return a signed JWT with a fixed expiry window."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": user_id, "email": email, "exp": expire},
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

    header = jwt.get_unverified_header(access_token)
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


def lookup_google_linked_user(*, access_token: str, db: Session) -> dict:
    """
    After Google OAuth, check whether this Supabase user is already linked to a Jorna row.
    If linked, returns a FastAPI JWT for API access.
    """
    claims = _decode_supabase_access_token(access_token)
    sub = (claims.get("sub") or "").lower()
    if not sub:
        raise AuthError(401, "Invalid token: missing sub")
    email = claims.get("email")
    user = db.query(User).filter(User.supabase_user_id == sub).first()
    if not user:
        return {"linked": False, "email": email}
    token = _make_token(user.user_id, user.email)
    return {
        "linked": True,
        "access_token": token,
        "token_type": "bearer",
        "user_id": user.user_id,
        "email": user.email,
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
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"user_id": user.user_id, "email": user.email}


def change_password(*, user_id: str, current_password: str, new_password: str, db: Session) -> dict:
    """Verify the current password then store a new bcrypt hash."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise AuthError(404, "User not found")
    if not bcrypt.checkpw(current_password.encode(), user.password.encode()):
        raise AuthError(401, "Current password is incorrect")
    user.password = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    db.commit()
    return {"message": "Password updated successfully"}


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

    token = _make_token(user.user_id, user.email)
    return {"access_token": token, "token_type": "bearer"}
