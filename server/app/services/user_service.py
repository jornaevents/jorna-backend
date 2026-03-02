"""Business logic for user profile management."""

from sqlalchemy.orm import Session

from app.db.models import User


class UserError(Exception):
    """Raised when a user operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _user_dict(user: User) -> dict:
    return {
        "user_id": user.user_id,
        "email": user.email,
        "username": user.username,
        "phone": user.phone,
        "f_name": user.f_name,
        "l_name": user.l_name,
        "age": user.age,
        "location": user.location,
        "gender": user.gender,
        "language": user.language,
        "pfp_url": user.pfp_url,
    }


def get_user(*, user_id: str, db: Session) -> dict:
    """Return the profile for *user_id*. Raises 404 if not found."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise UserError(404, "User not found")
    return _user_dict(user)


def update_user(*, user_id: str, update_data: dict, db: Session) -> dict:
    """Apply *update_data* (partial) to the user and return the updated profile."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise UserError(404, "User not found")
    for field, value in update_data.items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return _user_dict(user)
