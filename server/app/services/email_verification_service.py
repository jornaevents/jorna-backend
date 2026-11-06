"""An account proves it can read its email with a 6-digit code
(docs/DECISIONS.md #44). Clients are asked right after sign-up; the inquiry
form needs it, since its confirmation repeats what they sent.

The same shape as signing codes (signing_evidence): only a hash is kept, a
new code retires the old one, ten minutes to use it, five wrong guesses and
it's gone. The staging test code (config.SIGNING_TEST_CODE) works here too,
so the E2E suite can sign up without reading email.
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import config
from app.db.models import EmailCode, User
from app.services.email_service import send_email
from app.services.signing_evidence import mask_email

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5


class EmailVerificationError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(code_id: str, code: str) -> str:
    return hmac.new(config.SECRET_KEY.encode(), f"{code_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _open_codes(db: Session, user: User):
    return db.query(EmailCode).filter(EmailCode.user_id == user.user_id, EmailCode.used_at.is_(None))


def _verified(user: User) -> dict:
    return {"email_verified": True, "email": user.email}


def send_code(db: Session, user: User) -> dict:
    """Email a fresh code to the account's address; an earlier one stops
    working. Already verified is an answer, not an error."""
    if user.email_verified_at is not None:
        return _verified(user)
    now = _now()
    _open_codes(db, user).update({EmailCode.used_at: now}, synchronize_session=False)
    code = f"{secrets.randbelow(10 ** 6):06d}"
    row = EmailCode(
        user_id=user.user_id, email=user.email, code_hash="",
        expires_at=now + CODE_TTL, attempts=0, created_at=now,
    )
    db.add(row)
    db.flush()
    row.code_hash = _hash(row.code_id, code)
    sent = send_email(
        to=user.email,
        subject=f"{code} is your Jorna code",
        html=(
            "<p>Enter this code to confirm your email for Jorna:</p>"
            f"<p style=\"font-size:28px;letter-spacing:4px;font-weight:bold\">{code}</p>"
            "<p>It works for 10 minutes. If you didn't make a Jorna account, you can ignore this email.</p>"
        ),
    )
    # As with signing codes: staging has no email, and its test code works anyway.
    if not sent.get("success") and not config.SIGNING_TEST_CODE:
        db.rollback()
        raise EmailVerificationError(502, "We couldn't send the code. Check your email address and try again")
    db.commit()
    return {
        "email_verified": False,
        "sent_to": mask_email(user.email),
        "expires_in_minutes": int(CODE_TTL.total_seconds() // 60),
    }


def verify_code(db: Session, user: User, code: str) -> dict:
    """Mark the account verified if `code` is its latest one. A wrong guess
    is counted."""
    if user.email_verified_at is not None:
        return _verified(user)
    code = (code or "").strip()
    now = _now()
    if config.SIGNING_TEST_CODE and hmac.compare_digest(code, config.SIGNING_TEST_CODE):
        user.email_verified_at = now
        _open_codes(db, user).update({EmailCode.used_at: now}, synchronize_session=False)
        db.commit()
        return _verified(user)
    row = _open_codes(db, user).order_by(EmailCode.created_at.desc()).first()
    if row is None or row.expires_at < now:
        raise EmailVerificationError(400, "That code has expired. Send yourself a new one")
    if row.email.strip().lower() != (user.email or "").strip().lower():
        raise EmailVerificationError(400, "Your email changed since the code was sent. Send a new one")
    if not hmac.compare_digest(row.code_hash, _hash(row.code_id, code)):
        row.attempts = (row.attempts or 0) + 1
        if row.attempts >= MAX_ATTEMPTS:
            row.used_at = now
            db.commit()
            raise EmailVerificationError(400, "Too many wrong codes. Send yourself a new one")
        db.commit()
        raise EmailVerificationError(400, "That code isn't right. Check the email we sent and try again")
    row.used_at = now
    user.email_verified_at = now
    db.commit()
    return _verified(user)
