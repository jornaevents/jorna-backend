"""Business logic for user authentication (register & login)."""

import bcrypt
import jwt
from sqlalchemy.orm import Session

from app.db.models import User

SECRET_KEY = "your-secret-key-change-in-production"
ALGORITHM = "HS256"


class AuthError(Exception):
    """Raised when an auth operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def register_user(
    *,
    email: str,
    password: str,
    username: str,
    phone: str,
    f_name: str,
    l_name: str,
    age: int,
    location: str,
    gender: str,
    language: str,
    db: Session,
) -> dict:
    """Create a new user account. Returns ``{user_id, email}``."""
    existing = db.query(User).filter(
        (User.email == email) | (User.username == username)
    ).first()
    if existing:
        raise AuthError(400, "Email or username already taken")

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
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"user_id": user.user_id, "email": user.email}


def login_user(*, email: str, password: str, db: Session) -> dict:
    """Verify credentials and return a JWT.  Returns ``{access_token, token_type}``."""
    user = db.query(User).filter(User.email == email).first()
    if not user or not bcrypt.checkpw(password.encode(), user.password.encode()):
        raise AuthError(401, "Invalid email or password")

    token = jwt.encode(
        {"sub": user.user_id, "email": user.email},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    return {"access_token": token, "token_type": "bearer"}
