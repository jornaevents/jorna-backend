"""The vendor's signature, after the client's (docs/DECISIONS.md #34).

A contract (or an attached addendum or cancellation) is signed by the client
first, by link (guest_booking_service / document_service), and then waits as
"awaiting_vendor": frozen, its date held with no expiry. The vendor then
either countersigns, which books it and sends both sides the signed PDF, or
declines to sign with a reason, which voids it and frees the date.

The vendor signs signed in, so there's no emailed code: the evidence is the
account, the typed name, the e-records consent (asked once per wording and
remembered on the vendor), and where they signed from. It goes into the
signed snapshot beside the client's, so the final fingerprint covers both.

A vendor who hasn't signed is reminded a day and three days after the client
did, then not again; nothing expires.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.db.models import Booking, ContractDocument, ContractEvent, Service, User, Vendor
from app.models.schemas import BookingStatus, RejectionReason
from app.services import contract_document as doc
from app.services import esign_consent
from app.services.booking_service import LOCKED_BOOKING_STATUSES
from app.services.email_service import send_email
from app.utils.notifications import notify_vendor_contract_event

logger = logging.getLogger(__name__)

AWAITING = "awaiting_vendor"
MAX_REASON = 500

# Hours after the client's signature, latest first: a sweep that finds both
# due sends only the later one.
REMINDERS = (("day3", timedelta(days=3)), ("day1", timedelta(days=1)))


class CountersignError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _vendor_user(vendor: Vendor | None, db: Session) -> User | None:
    return db.query(User).filter(User.user_id == vendor.user_id).first() if vendor else None


def _display_name(user: User | None) -> str:
    return (f"{user.f_name or ''} {user.l_name or ''}".strip() if user else "") or "Your vendor"


def _own_vendor(vendor_id: str, caller_user_id: str, db: Session) -> Vendor:
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor or vendor.user_id != caller_user_id:
        raise CountersignError(404, "Contract not found")
    return vendor


def consent_view(vendor: Vendor) -> dict:
    """What the countersign screen needs to ask: the words, and whether
    this vendor has already agreed to this version of them."""
    return {**esign_consent.view(), "given": vendor.esign_consent_version == esign_consent.VERSION}


def _consent(vendor: Vendor, consent: bool, consent_version: str | None) -> dict:
    """The vendor's e-records consent for this signature: remembered from
    an earlier one, or given now (and remembered from then on)."""
    if vendor.esign_consent_version == esign_consent.VERSION:
        return {
            "version": esign_consent.VERSION,
            "text": esign_consent.TEXT,
            "accepted_at": vendor.esign_consent_at.isoformat() if vendor.esign_consent_at else None,
        }
    if not consent:
        raise CountersignError(400, "Tick the box to agree to sign electronically")
    if consent_version != esign_consent.VERSION:
        raise CountersignError(409, "The signing terms on this page are out of date. Reload and sign again")
    now = _now()
    vendor.esign_consent_version = esign_consent.VERSION
    vendor.esign_consent_at = now
    return {"version": esign_consent.VERSION, "text": esign_consent.TEXT, "accepted_at": now.isoformat()}


def _evidence(vendor: Vendor, user: User | None, client: dict | None, consent: dict) -> dict:
    client = client or {}
    return {
        "signed_in_as": {"user_id": vendor.user_id, "vendor_id": vendor.vendor_id,
                         "email": getattr(user, "email", None)},
        "ip": client.get("ip"),
        "user_agent": client.get("user_agent"),
        "consent": consent,
    }


def _typed_name(signer_name: str | None) -> str:
    name = (signer_name or "").strip()
    if not name:
        raise CountersignError(400, "Type your full legal name to sign")
    return name[:255]


def _reason(reason: str | None) -> str:
    text = (reason or "").strip()
    if not text:
        raise CountersignError(400, "Tell your client why you're not signing")
    return text[:MAX_REASON]


# ── Contracts ────────────────────────────────────────────────────────

def _awaiting_booking(booking_id: str, caller_user_id: str, db: Session) -> tuple[Booking, Vendor]:
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking or not booking.contract_token:
        raise CountersignError(404, "Contract not found")
    vendor = _own_vendor(booking.vendor_id, caller_user_id, db)
    if booking.signed_at is not None:
        raise CountersignError(400, "This contract is already signed by both of you")
    if booking.status == BookingStatus.REJECTED.value:
        raise CountersignError(400, "This contract is no longer live")
    if booking.contract_status != AWAITING or booking.client_signed_at is None:
        raise CountersignError(400, "Your client hasn't signed this yet")
    return booking, vendor


def _contract_result(booking: Booking, vendor: Vendor, db: Session) -> dict:
    from app.services.contract_service import _contract_dict

    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    return _contract_dict(booking, service, vendor, db)


def countersign_contract(
    *, booking_id: str, caller_user_id: str, signer_name: str | None, db: Session,
    consent: bool = False, consent_version: str | None = None, client: dict | None = None,
) -> dict:
    """The vendor signs what the client signed. It's booked from here: the
    deposit's "on signing" date, reminders and the ledger all count from
    signed_at, which is set now."""
    booking, vendor = _awaiting_booking(booking_id, caller_user_id, db)
    name = _typed_name(signer_name)
    user = _vendor_user(vendor, db)
    evidence = _evidence(vendor, user, client, _consent(vendor, consent, consent_version))

    now = datetime.now(timezone.utc)
    client_sha = booking.signed_snapshot_sha256
    booking.signed_snapshot, booking.signed_snapshot_sha256 = doc.countersignature(
        booking.signed_snapshot or {}, client_sha,
        signer_name=name, provider=_display_name(user), signed_at=now, evidence=evidence,
    )
    booking.vendor_signer_name = name
    booking.vendor_signed_at = now
    booking.vendor_sign_evidence = evidence
    booking.signed_at = now
    booking.contract_status = "signed"
    # They've just read and signed it: not new to them on the Bookings tab
    # (DECISIONS #30), as a contract booked by the client's signature alone was.
    booking.vendor_seen_at = booking.vendor_seen_at or now
    doc.record(db, booking, "countersigned", "vendor", {
        "signer_name": name, "sha256": booking.signed_snapshot_sha256,
        "client_sha256": client_sha, "ip": evidence["ip"],
    })
    db.commit()
    db.refresh(booking)

    from app.services.guest_booking_service import _send_signed_copies

    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    _send_signed_copies(booking, service, vendor, user, _display_name(user), db)
    return _contract_result(booking, vendor, db)


def decline_contract_signature(*, booking_id: str, caller_user_id: str, reason: str | None, db: Session) -> dict:
    """The vendor won't sign what the client signed. The contract is voided,
    which frees the date, and the client is told why."""
    booking, vendor = _awaiting_booking(booking_id, caller_user_id, db)
    note = _reason(reason)
    booking.status = BookingStatus.REJECTED.value
    booking.rejected_reason = RejectionReason.VENDOR_DECLINED_SIGNATURE.value
    booking.vendor_decline_note = note
    booking.contract_status = "voided"
    booking.voided_at = _now()
    doc.record(db, booking, "signature_declined", "vendor", {"reason": note})
    db.commit()
    db.refresh(booking)

    name = _display_name(_vendor_user(vendor, db))
    if booking.guest_email:
        send_email(
            to=booking.guest_email,
            subject=f"{name} didn't sign your contract",
            html=(
                f"<p>{name} decided not to sign the contract you signed for {booking.date_iso}, "
                "so it isn't booked and nothing is owed.</p>"
                f"<p>They said: “{note}”</p>"
                "<p>You can reply to them, or look for another vendor for that date.</p>"
            ),
        )
    return _contract_result(booking, vendor, db)


# ── Attached documents ───────────────────────────────────────────────

def _awaiting_document(document_id: str, caller_user_id: str, db: Session) -> tuple[ContractDocument, Booking, Vendor]:
    d = db.query(ContractDocument).filter(ContractDocument.document_id == document_id).first()
    if not d:
        raise CountersignError(404, "Document not found")
    vendor = _own_vendor(d.vendor_id, caller_user_id, db)
    if d.status == "signed":
        raise CountersignError(400, "This document is already signed by both of you")
    if d.status != AWAITING or d.client_signed_at is None:
        raise CountersignError(400, "Your client hasn't signed this yet")
    booking = db.query(Booking).filter(Booking.booking_id == d.booking_id).first()
    return d, booking, vendor


def countersign_document(
    *, document_id: str, caller_user_id: str, signer_name: str | None, db: Session,
    consent: bool = False, consent_version: str | None = None, client: dict | None = None,
) -> dict:
    from app.services import document_service

    d, booking, vendor = _awaiting_document(document_id, caller_user_id, db)
    name = _typed_name(signer_name)
    user = _vendor_user(vendor, db)
    evidence = _evidence(vendor, user, client, _consent(vendor, consent, consent_version))

    now = _now()
    client_sha = d.signed_snapshot_sha256
    d.signed_snapshot, d.signed_snapshot_sha256 = doc.countersignature(
        d.signed_snapshot or {}, client_sha,
        signer_name=name, provider=_display_name(user), signed_at=now, evidence=evidence,
    )
    d.vendor_signer_name = name
    d.vendor_signed_at = now
    d.vendor_sign_evidence = evidence
    d.signed_at = now
    d.status = "signed"
    d.updated_at = now
    doc.record(db, booking, "document_countersigned", "vendor", {
        "document_id": d.document_id, "title": d.title, "signer_name": name,
        "sha256": d.signed_snapshot_sha256, "client_sha256": client_sha, "ip": evidence["ip"],
    })
    db.commit()
    db.refresh(d)

    document_service.send_signed_copies(d, booking, document_service._client_email(booking, db), db)
    return document_service._dict(d, booking, db)


def decline_document_signature(*, document_id: str, caller_user_id: str, reason: str | None, db: Session) -> dict:
    from app.services import document_service

    d, booking, vendor = _awaiting_document(document_id, caller_user_id, db)
    note = _reason(reason)
    now = _now()
    d.status = "voided"
    d.voided_at = now
    d.updated_at = now
    doc.record(db, booking, "document_signature_declined", "vendor", {
        "document_id": d.document_id, "title": d.title, "reason": note,
    })
    db.commit()
    db.refresh(d)

    to = document_service._client_email(booking, db)
    if to:
        name = _display_name(_vendor_user(vendor, db))
        send_email(
            to=to,
            subject=f"{name} didn't sign “{d.title}”",
            html=(
                f"<p>{name} decided not to sign “{d.title}”, which you signed for your booking on "
                f"{booking.date_iso}. It doesn't apply, and your booking is unchanged.</p>"
                f"<p>They said: “{note}”</p>"
            ),
        )
    return document_service._dict(d, booking, db)


# ── Reminders ────────────────────────────────────────────────────────

def _reminded(db: Session, booking_id: str) -> set[tuple[str | None, str]]:
    rows = db.query(ContractEvent).filter(
        ContractEvent.booking_id == booking_id, ContractEvent.kind == "countersign_reminder",
    ).all()
    return {((r.detail or {}).get("document_id"), (r.detail or {}).get("which")) for r in rows}


def _which(signed: datetime, now: datetime) -> str | None:
    for which, after in REMINDERS:
        if now - signed >= after:
            return which
    return None


def _remind(db: Session, booking: Booking, which: str, *, who: str | None, title: str,
            document_id: str | None = None) -> None:
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    days = "3 days" if which == "day3" else "a day"
    notify_vendor_contract_event(
        vendor_user=_vendor_user(vendor, db),
        title=f"{who or 'Your client'} signed {days} ago — add your signature",
        body=f"{title} for {booking.date_iso}. It's booked once you sign too.",
        booking_id=booking.booking_id, event="countersign_reminder", db=db,
    )
    doc.record(db, booking, "countersign_reminder", "system", {
        "which": which, **({"document_id": document_id} if document_id else {}),
    })


def send_countersign_reminders(*, db: Session, now: datetime | None = None) -> int:
    """One sweep, idempotent through the countersign_reminder events it
    records. Returns how many went out."""
    now = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    sent = 0
    bookings = db.query(Booking).filter(
        Booking.contract_status == AWAITING,
        Booking.signed_at.is_(None),
        Booking.client_signed_at.isnot(None),
        Booking.status.in_(LOCKED_BOOKING_STATUSES),
    ).all()
    for booking in bookings:
        try:
            which = _which(booking.client_signed_at.replace(tzinfo=None), now)
            if which and (None, which) not in _reminded(db, booking.booking_id):
                _remind(db, booking, which, who=booking.signer_name, title=booking.document_title or "Your contract")
                db.commit()
                sent += 1
        except Exception as exc:  # one bad contract must not stop the rest
            logger.warning("Countersign reminder for booking %s failed: %s", booking.booking_id, exc)
            db.rollback()
    documents = db.query(ContractDocument).filter(
        ContractDocument.status == AWAITING, ContractDocument.client_signed_at.isnot(None),
    ).all()
    for d in documents:
        try:
            which = _which(d.client_signed_at.replace(tzinfo=None), now)
            if which and (d.document_id, which) not in _reminded(db, d.booking_id):
                booking = db.query(Booking).filter(Booking.booking_id == d.booking_id).first()
                _remind(db, booking, which, who=d.signer_name, title=f"“{d.title}”", document_id=d.document_id)
                db.commit()
                sent += 1
        except Exception as exc:
            logger.warning("Countersign reminder for document %s failed: %s", d.document_id, exc)
            db.rollback()
    if sent:
        logger.info("Sent %d countersign reminders", sent)
    return sent
