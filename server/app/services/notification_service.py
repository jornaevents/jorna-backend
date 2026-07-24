"""Business logic for push-token management (the push_tokens table)."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import PushToken, User


class NotificationServiceError(Exception):
    """Raised when a notification-service operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def register_fcm_token(
    *, user_id: str, fcm_token: str, platform: str = "ios", db: Session
) -> dict:
    """Register a device's push token for this user.

    Tokens are globally unique (one physical device). Re-registering an existing
    token — a phone that re-logs-in as someone else, say — reassigns it to the
    current user rather than duplicating, so a device only ever pushes for whoever
    is signed in on it now.
    """
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise NotificationServiceError(404, "User not found")

    existing = db.query(PushToken).filter(PushToken.token == fcm_token).first()
    if existing:
        existing.user_id = user_id
        existing.platform = platform
        existing.last_used_at = datetime.utcnow()
    else:
        db.add(PushToken(user_id=user_id, token=fcm_token, platform=platform))
    db.commit()
    return {"message": "Push token registered successfully", "user_id": user_id}


def remove_fcm_token(*, user_id: str, token: str | None = None, db: Session) -> dict:
    """Opt out of push. With a token, removes just that device; without one,
    removes every device for the user (a full opt-out)."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise NotificationServiceError(404, "User not found")

    q = db.query(PushToken).filter(PushToken.user_id == user_id)
    if token:
        q = q.filter(PushToken.token == token)
    q.delete(synchronize_session=False)
    db.commit()
    return {"message": "Push token(s) removed successfully", "user_id": user_id}


def get_token_status(*, user_id: str, db: Session) -> dict:
    """Whether the user has at least one device registered for push."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise NotificationServiceError(404, "User not found")

    has_token = (
        db.query(PushToken.id).filter(PushToken.user_id == user_id).first() is not None
    )
    return {"user_id": user_id, "has_token": has_token}
