"""Shared FastAPI dependencies (e.g. JWT authentication)."""

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User
from app.config import ALGORITHM, SECRET_KEY

security = HTTPBearer()
optional_security = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """Extract JWT from Authorization header, decode it, and return the User.

    Raises HTTP 401 if the token is missing, invalid, or the user no longer exists.
    """
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        token_version = payload.get("tv")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired — please log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if token_version != user.token_version:
        raise HTTPException(status_code=401, detail="Token has been revoked — please log in again")
    return user


def user_from_token(token: str | None, db: Session) -> User | None:
    """Decode a bearer token and return the matching User, or None if invalid.

    The non-raising counterpart to get_current_user, for auth contexts that
    can't use HTTP exceptions — notably the chat WebSocket, which must close the
    socket with a policy-violation code rather than return a 401 body.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.InvalidTokenError:
        return None
    user = db.query(User).filter(User.user_id == payload.get("sub")).first()
    if not user or payload.get("tv") != user.token_version:
        return None
    return user


def get_current_admin(current_user: User = Depends(get_current_user)) -> User:
    """Require the authenticated user to have is_admin=True. Returns 403 otherwise."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_security),
    db: Session = Depends(get_db),
) -> User | None:
    """The signed-in user if a valid bearer token came with the request, else
    None — for public endpoints that show the owner a little more (e.g. a
    vendor's own hidden packages) without requiring anyone to sign in."""
    return user_from_token(credentials.credentials if credentials else None, db)
