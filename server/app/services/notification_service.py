"""Business logic for FCM token management."""

from sqlalchemy.orm import Session

from app.db.models import User


class NotificationServiceError(Exception):
    """Raised when a notification-service operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def register_fcm_token(*, user_id: str, fcm_token: str, db: Session) -> dict:
    """Save or update the user's FCM device token."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise NotificationServiceError(404, "User not found")

    user.fcm_token = fcm_token
    db.commit()
    return {"message": "FCM token registered successfully", "user_id": user.user_id}


def remove_fcm_token(*, user_id: str, db: Session) -> dict:
    """Clear the user's FCM token (opt-out of notifications)."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise NotificationServiceError(404, "User not found")

    user.fcm_token = None
    db.commit()
    return {"message": "FCM token removed successfully", "user_id": user.user_id}


def get_token_status(*, user_id: str, db: Session) -> dict:
    """Check whether the user has a registered FCM token."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise NotificationServiceError(404, "User not found")

    return {"user_id": user.user_id, "has_token": user.fcm_token is not None}
