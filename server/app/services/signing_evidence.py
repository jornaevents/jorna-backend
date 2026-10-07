"""What a signature by link carries besides the typed name
(docs/DECISIONS.md #27): the address and browser it came from, the
e-records consent the client ticked (its exact words), and proof they could
read the contract's email — a 6-digit code sent there just before.

All of it goes into the signed snapshot, so the snapshot's SHA-256 covers
it, and onto the PDF's signing certificate.

Shared by the contract (guest_booking_service) and attached documents
(document_service); each maps SigningError onto its own error type."""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import config
from app.db.models import Booking, SigningCode
from app.services import contract_document as doc
from app.services import esign_consent
from app.services.email_service import send_email

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5


class SigningError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(code_id: str, code: str) -> str:
    return hmac.new(config.SECRET_KEY.encode(), f"{code_id}:{code}".encode(), hashlib.sha256).hexdigest()


def mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:1]}{'•' * max(len(name) - 1, 2)}@{domain}" if domain else email


def _open_codes(db: Session, booking: Booking, document_id: str | None):
    return db.query(SigningCode).filter(
        SigningCode.booking_id == booking.booking_id,
        SigningCode.document_id.is_(None) if document_id is None else SigningCode.document_id == document_id,
        SigningCode.used_at.is_(None),
    )


def send_code(
    db: Session, booking: Booking, *, email: str | None, vendor_name: str,
    what: str, document_id: str | None = None,
) -> dict:
    """Email a fresh code; any earlier one for the same agreement stops
    working. `what` names the agreement in the email ("your contract")."""
    if not email:
        raise SigningError(400, "Add your email first so we can send you a code")
    now = _now()
    _open_codes(db, booking, document_id).update({SigningCode.used_at: now}, synchronize_session=False)
    code = f"{secrets.randbelow(10 ** 6):06d}"
    row = SigningCode(
        booking_id=booking.booking_id, document_id=document_id, email=email,
        code_hash="", expires_at=now + CODE_TTL, attempts=0, created_at=now,
    )
    db.add(row)
    db.flush()
    row.code_hash = _hash(row.code_id, code)
    sent = send_email(
        to=email,
        subject=f"{code} is your code to sign with {vendor_name}",
        html=(
            f"<p>Your code to sign {what} with {vendor_name} is:</p>"
            f"<p style=\"font-size:28px;letter-spacing:4px;font-weight:bold\">{code}</p>"
            "<p>It works for 10 minutes. If you didn't ask for it, you can ignore this email — "
            "nobody can sign without it.</p>"
        ),
    )
    if not sent.get("success"):
        db.rollback()
        raise SigningError(502, "We couldn't send the code. Check your email address and try again")
    doc.record(db, booking, "code_sent", "system", {
        "email": email, "document_id": document_id, "email_id": sent.get("id"),
    })
    db.commit()
    return {"sent_to": mask_email(email), "expires_in_minutes": int(CODE_TTL.total_seconds() // 60)}


def _verify(db: Session, booking: Booking, *, code: str, email: str, document_id: str | None) -> datetime:
    """The code's own sent time on success. A wrong guess is counted (and
    committed, since the caller's error rolls back everything else)."""
    if config.SIGNING_TEST_CODE and hmac.compare_digest(code, config.SIGNING_TEST_CODE):
        return _now()
    row = _open_codes(db, booking, document_id).order_by(SigningCode.created_at.desc()).first()
    now = _now()
    if row is None or row.expires_at < now:
        raise SigningError(400, "That code has expired. Send yourself a new one")
    if row.email.strip().lower() != email.strip().lower():
        raise SigningError(400, "Your email changed since the code was sent. Send a new one")
    if not hmac.compare_digest(row.code_hash, _hash(row.code_id, code)):
        row.attempts = (row.attempts or 0) + 1
        if row.attempts >= MAX_ATTEMPTS:
            row.used_at = now
            db.commit()
            raise SigningError(400, "Too many wrong codes. Send yourself a new one")
        db.commit()
        raise SigningError(400, "That code isn't right. Check the email we sent and try again")
    row.used_at = now
    return now


def collect(
    db: Session, booking: Booking, *, email: str | None, client: dict | None,
    code: str | None, consent: bool, consent_version: str | None, document_id: str | None = None,
) -> dict:
    """Check the code and consent, and return the evidence the snapshot
    keeps. Call it after every other check on the signature, so a refusal
    for another reason doesn't spend the code. With SIGNING_REQUIRE_CODE
    off, an older page that sends neither still signs (recorded as such)."""
    if consent:
        if consent_version != esign_consent.VERSION:
            raise SigningError(409, "The signing terms on this page are out of date. Reload and sign again")
    elif config.SIGNING_REQUIRE_CODE:
        raise SigningError(400, "Tick the box to agree to sign electronically")
    code = (code or "").strip()
    verified_at = None
    if code:
        if not email:
            raise SigningError(400, "Add your email first so we can send you a code")
        verified_at = _verify(db, booking, code=code, email=email, document_id=document_id)
    elif config.SIGNING_REQUIRE_CODE:
        raise SigningError(400, "Enter the code we emailed you")
    client = client or {}
    return {
        "ip": client.get("ip"),
        "user_agent": client.get("user_agent"),
        "email": email,
        "email_verified_at": verified_at.isoformat() if verified_at else None,
        "consent": (
            {"version": esign_consent.VERSION, "text": esign_consent.TEXT, "accepted_at": _now().isoformat()}
            if consent else None
        ),
    }
