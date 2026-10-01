"""Addenda and cancellation agreements attached to a signed booking (0067,
docs/DECISIONS.md #21).

Text only: a document never changes the booking's price, date or hold —
an addendum that does is v2. It has its own no-login link (same trust model
as a contract's, DECISIONS #13: the token is the credential), a typed
signature, and a frozen, hashed snapshot of exactly what was signed. Each
step lands on the parent booking's contract timeline.
"""

import hashlib
import json
import secrets
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import WEB_APP_URL
from app.db.models import Booking, ContractDocument, User, Vendor
from app.models.schemas import BookingStatus
from app.services import contract_document as doc
from app.services.email_service import send_email
from app.utils.timeutil import utc_iso

KINDS = ("addendum", "cancellation")
KIND_LABEL = {"addendum": "addendum", "cancellation": "cancellation agreement"}


class DocumentServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _link(d: ContractDocument) -> str:
    return f"{WEB_APP_URL}/booking-link?d={d.token}"


def _sections(raw: list | None) -> list[dict]:
    try:
        sections = doc.normalize_clauses(raw)
    except doc.DocumentError as e:
        raise DocumentServiceError(400, e.detail) from e
    if not sections:
        raise DocumentServiceError(400, "Add at least one section of text")
    return sections


def _title(raw: str | None, kind: str) -> str:
    title = (raw or "").strip()[:200]
    return title or ("Service addendum" if kind == "addendum" else "Cancellation agreement")


def _client_email(booking: Booking, db: Session) -> str | None:
    if booking.guest_email:
        return booking.guest_email
    if booking.user_id:
        user = db.query(User).filter(User.user_id == booking.user_id).first()
        return user.email if user else None
    return None


def _client_name(booking: Booking, db: Session) -> str | None:
    if booking.guest_name or booking.signer_name:
        return booking.guest_name or booking.signer_name
    if booking.user_id:
        user = db.query(User).filter(User.user_id == booking.user_id).first()
        return f"{user.f_name} {user.l_name}".strip() if user else None
    return None


def _vendor_name(vendor: Vendor | None, db: Session) -> str | None:
    if not vendor:
        return None
    user = db.query(User).filter(User.user_id == vendor.user_id).first()
    return f"{user.f_name} {user.l_name}".strip() if user else None


def _dict(d: ContractDocument, booking: Booking, db: Session, *, with_token: bool = True) -> dict:
    vendor = db.query(Vendor).filter(Vendor.vendor_id == d.vendor_id).first()
    out = {
        "document_id": d.document_id,
        "booking_id": d.booking_id,
        "kind": d.kind,
        "title": d.title,
        "sections": d.sections,
        "status": d.status,
        "sent_at": utc_iso(d.sent_at),
        "viewed_at": utc_iso(d.viewed_at),
        "signed_at": utc_iso(d.signed_at),
        "signer_name": d.signer_name,
        "declined_at": utc_iso(d.declined_at),
        "decline_reason": d.decline_reason,
        "voided_at": utc_iso(d.voided_at),
        "signed_snapshot_sha256": d.signed_snapshot_sha256,
        "created_at": utc_iso(d.created_at),
        "updated_at": utc_iso(d.updated_at),
        # What it's attached to, so the signing page can say which booking.
        "vendor_display_name": _vendor_name(vendor, db),
        "client_name": _client_name(booking, db),
        "date_iso": booking.date_iso,
        "location": booking.location,
    }
    if with_token:
        out["token"] = d.token
    return out


def _own_vendor(caller_user_id: str, vendor_id: str, db: Session) -> Vendor:
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor or vendor.user_id != caller_user_id:
        raise DocumentServiceError(404, "Booking not found")
    return vendor


def _own_booking(booking_id: str, caller_user_id: str, db: Session) -> tuple[Booking, Vendor]:
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise DocumentServiceError(404, "Booking not found")
    vendor = _own_vendor(caller_user_id, booking.vendor_id, db)
    return booking, vendor


def _own_document(document_id: str, caller_user_id: str, db: Session) -> tuple[ContractDocument, Booking]:
    d = db.query(ContractDocument).filter(ContractDocument.document_id == document_id).first()
    if not d:
        raise DocumentServiceError(404, "Document not found")
    booking, _ = _own_booking(d.booking_id, caller_user_id, db)
    return d, booking


def _agreed(booking: Booking) -> bool:
    """A document attaches to something already agreed: a signed contract,
    or a marketplace booking accepted before contracts existed."""
    if booking.status == BookingStatus.REJECTED.value or booking.cancelled_at is not None:
        return False
    if booking.contract_token:
        return booking.signed_at is not None
    return booking.status in (BookingStatus.APPROVED.value, BookingStatus.PAYMENT_CONFIRMED.value)


def _deliver(d: ContractDocument, booking: Booking, vendor: Vendor, db: Session, email_client: bool) -> None:
    now = _now()
    d.status = "viewed" if d.viewed_at else "sent"
    d.sent_at = now
    d.updated_at = now
    doc.record(db, booking, "document_sent", "vendor", {"document_id": d.document_id, "kind": d.kind, "title": d.title})
    if email_client:
        to = _client_email(booking, db)
        if not to:
            raise DocumentServiceError(400, "There's no email for this client — copy the link and send it yourself")
        name = _vendor_name(vendor, db) or "Your vendor"
        send_email(
            to=to,
            subject=f"{name} sent you {('an ' if d.kind == 'addendum' else 'a ')}{KIND_LABEL[d.kind]} to sign",
            html=(
                f"<p>{name} sent you “{d.title}” for your booking on {booking.date_iso}.</p>"
                f'<p><a href="{_link(d)}">Review and sign</a> — no account needed.</p>'
            ),
        )
        doc.record(db, booking, "document_emailed", "vendor", {"document_id": d.document_id, "to": to})


# ── Vendor ───────────────────────────────────────────────────────────

def create_document(
    *, booking_id: str, caller_user_id: str, kind: str, title: str | None, sections: list,
    send: bool, email_client: bool, db: Session,
) -> dict:
    booking, vendor = _own_booking(booking_id, caller_user_id, db)
    if kind not in KINDS:
        raise DocumentServiceError(400, "A document is an addendum or a cancellation agreement")
    if not _agreed(booking):
        raise DocumentServiceError(400, "Documents attach to a signed booking")
    now = _now()
    d = ContractDocument(
        booking_id=booking.booking_id, vendor_id=vendor.vendor_id, kind=kind,
        title=_title(title, kind), sections=_sections(sections), token=secrets.token_urlsafe(24),
        status="draft", created_at=now, updated_at=now,
    )
    db.add(d)
    db.flush()
    doc.record(db, booking, "document_created", "vendor", {"document_id": d.document_id, "kind": kind, "title": d.title})
    if send:
        _deliver(d, booking, vendor, db, email_client)
    db.commit()
    db.refresh(d)
    return _dict(d, booking, db)


def update_document(
    *, document_id: str, caller_user_id: str, title: str | None, sections: list | None, db: Session,
) -> dict:
    d, booking = _own_document(document_id, caller_user_id, db)
    if d.status in ("signed", "declined", "voided"):
        raise DocumentServiceError(400, f"This document was {d.status} and can no longer be edited")
    if title is not None:
        d.title = _title(title, d.kind)
    if sections is not None:
        d.sections = _sections(sections)
    d.updated_at = _now()
    doc.record(db, booking, "document_edited", "vendor", {"document_id": d.document_id})
    db.commit()
    db.refresh(d)
    return _dict(d, booking, db)


def send_document(*, document_id: str, caller_user_id: str, email_client: bool, db: Session) -> dict:
    d, booking = _own_document(document_id, caller_user_id, db)
    if d.status in ("signed", "declined", "voided"):
        raise DocumentServiceError(400, f"This document was {d.status} and can't be sent")
    vendor = db.query(Vendor).filter(Vendor.vendor_id == d.vendor_id).first()
    _deliver(d, booking, vendor, db, email_client)
    db.commit()
    db.refresh(d)
    return _dict(d, booking, db)


def void_document(*, document_id: str, caller_user_id: str, db: Session) -> dict:
    d, booking = _own_document(document_id, caller_user_id, db)
    if d.status == "signed":
        raise DocumentServiceError(400, "A signed document can't be voided")
    if d.status != "voided":
        d.status = "voided"
        d.voided_at = _now()
        d.updated_at = d.voided_at
        doc.record(db, booking, "document_voided", "vendor", {"document_id": d.document_id})
        db.commit()
        db.refresh(d)
    return _dict(d, booking, db)


def list_documents(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    booking, _ = _own_booking(booking_id, caller_user_id, db)
    rows = (
        db.query(ContractDocument)
        .filter(ContractDocument.booking_id == booking.booking_id)
        .order_by(ContractDocument.created_at)
        .all()
    )
    return {"items": [_dict(d, booking, db) for d in rows], "total": len(rows)}


def document_pdf(*, document_id: str, caller_user_id: str, db: Session) -> tuple[bytes, str]:
    from app.services import pdf_service

    d, booking = _own_document(document_id, caller_user_id, db)
    return pdf_service.document_pdf(d, booking, db, _client_name(booking, db))


# ── The couple, by link ──────────────────────────────────────────────

def _by_token(token: str, db: Session) -> tuple[ContractDocument, Booking]:
    d = db.query(ContractDocument).filter(ContractDocument.token == token).first()
    # A draft isn't there yet, as far as the link goes.
    if not d or d.status == "draft":
        raise DocumentServiceError(404, "Document not found")
    booking = db.query(Booking).filter(Booking.booking_id == d.booking_id).first()
    return d, booking


def _require_open(d: ContractDocument) -> None:
    if d.status == "voided":
        raise DocumentServiceError(410, "This document was withdrawn by the vendor")
    if d.status == "declined":
        raise DocumentServiceError(410, "You declined this document")
    if d.status == "signed":
        raise DocumentServiceError(400, "This document has already been signed")


def get_by_token(*, token: str, preview: bool, db: Session) -> dict:
    """The couple opening their link. The first open marks it viewed; the
    vendor's own preview doesn't."""
    d, booking = _by_token(token, db)
    if not preview and d.viewed_at is None and d.status == "sent":
        d.status = "viewed"
        d.viewed_at = _now()
        doc.record(db, booking, "document_viewed", "client", {"document_id": d.document_id})
        db.commit()
        db.refresh(d)
    return _dict(d, booking, db, with_token=False)


def sign_by_token(*, token: str, signer_name: str, db: Session) -> dict:
    d, booking = _by_token(token, db)
    _require_open(d)
    if not signer_name or not signer_name.strip():
        raise DocumentServiceError(400, "Type your full legal name to sign")
    d.signer_name = signer_name.strip()[:255]
    d.signed_at = _now()
    d.status = "signed"
    d.updated_at = d.signed_at
    snapshot = {
        "document_id": d.document_id,
        "booking_id": d.booking_id,
        "kind": d.kind,
        "title": d.title,
        "sections": d.sections,
        "date_iso": booking.date_iso,
        "signer_name": d.signer_name,
        "signed_at": d.signed_at.isoformat(),
    }
    d.signed_snapshot = snapshot
    d.signed_snapshot_sha256 = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    doc.record(db, booking, "document_signed", "client", {
        "document_id": d.document_id, "title": d.title, "signer_name": d.signer_name,
        "sha256": d.signed_snapshot_sha256,
    })
    db.commit()
    db.refresh(d)

    from app.services.guest_booking_service import _tell_vendor

    _tell_vendor(
        booking, f"{d.signer_name} signed “{d.title}”",
        f"For the booking on {booking.date_iso}.", "document_signed", db,
    )
    return _dict(d, booking, db, with_token=False)


def decline_by_token(*, token: str, reason: str | None, db: Session) -> dict:
    d, booking = _by_token(token, db)
    _require_open(d)
    d.status = "declined"
    d.declined_at = _now()
    d.updated_at = d.declined_at
    d.decline_reason = (reason or "").strip()[:500] or None
    doc.record(db, booking, "document_declined", "client", {"document_id": d.document_id, "reason": d.decline_reason})
    db.commit()
    db.refresh(d)

    from app.services.guest_booking_service import _tell_vendor

    _tell_vendor(
        booking, f"“{d.title}” was declined",
        f"For the booking on {booking.date_iso}." + (f' They said: "{d.decline_reason}"' if d.decline_reason else ""),
        "document_declined", db,
    )
    return _dict(d, booking, db, with_token=False)


def guest_document_pdf(*, token: str, db: Session) -> tuple[bytes, str]:
    from app.services import pdf_service

    d, booking = _by_token(token, db)
    return pdf_service.document_pdf(d, booking, db, _client_name(booking, db))
