"""Addenda and cancellation agreements attached to a signed booking (0067,
docs/DECISIONS.md #21).

Text only: a document never changes the booking's price, date or hold —
an addendum that does is v2. It has its own no-login link (same trust model
as a contract's, DECISIONS #13: the token is the credential), a typed
signature, and a frozen, hashed snapshot of exactly what was signed. Each
step lands on the parent booking's contract timeline.
"""

import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import config
from app.config import WEB_APP_URL
from app.db.models import Booking, ContractDocument, ContractEvent, User, Vendor
from app.models.schemas import BookingStatus
from app.services import contract_document as doc
from app.services import esign_consent, signing_evidence
from app.services.email_service import pdf_attachment, send_email
from app.utils.timeutil import utc_iso

logger = logging.getLogger(__name__)

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


def _totals_view(d: ContractDocument, booking: Booking, db: Session) -> dict:
    """What the client sees before signing: "Total goes from $1,360 to
    $1,660". Once signed it's already applied, so the booking says it."""
    if not d.line_items and not d.removed_line_ids:
        return {"total_before": None, "total_after": None}
    if d.status == "signed":
        applied = db.query(ContractEvent).filter(
            ContractEvent.booking_id == booking.booking_id, ContractEvent.kind == "addendum_applied",
        ).all()
        mine = next((e.detail for e in applied if (e.detail or {}).get("document_id") == d.document_id), None)
        if mine:
            return {"total_before": mine.get("total_before"), "total_after": mine.get("total_after")}
    try:
        effect = _effect(d, booking, db)
    except DocumentServiceError:
        return {"total_before": booking.amount_cents, "total_after": None}
    return {"total_before": effect["total_before"], "total_after": effect["total_after"]}


def _vendor_refusal(d: ContractDocument, db: Session) -> str | None:
    events = (
        db.query(ContractEvent)
        .filter(ContractEvent.booking_id == d.booking_id, ContractEvent.kind == "document_signature_declined")
        .order_by(ContractEvent.at.desc())
        .all()
    )
    return next(((e.detail or {}).get("reason") for e in events if (e.detail or {}).get("document_id") == d.document_id), None)


def _dict(d: ContractDocument, booking: Booking, db: Session, *, with_token: bool = True) -> dict:
    vendor = db.query(Vendor).filter(Vendor.vendor_id == d.vendor_id).first()
    out = {
        "document_id": d.document_id,
        "booking_id": d.booking_id,
        "kind": d.kind,
        "title": d.title,
        "sections": d.sections,
        "line_items": d.line_items,
        "removed_line_ids": d.removed_line_ids,
        **_totals_view(d, booking, db),
        "changes": changes_view(d, booking, db),
        "status": d.status,
        "sent_at": utc_iso(d.sent_at),
        "viewed_at": utc_iso(d.viewed_at),
        # Signed by both (DECISIONS #34): signed_at is the vendor's
        # countersignature; client_signed_at and signer_name, the client's.
        "signed_at": utc_iso(d.signed_at),
        "signer_name": d.signer_name,
        "client_signed_at": utc_iso(d.client_signed_at),
        "vendor_signer_name": d.vendor_signer_name,
        "vendor_signed_at": utc_iso(d.vendor_signed_at),
        "declined_at": utc_iso(d.declined_at),
        "decline_reason": d.decline_reason,
        # The vendor's reason for not countersigning, kept on its event (no
        # column needed): a refused document showed only "Void" (2026-10-26).
        "vendor_decline_reason": _vendor_refusal(d, db) if d.status == "voided" and d.client_signed_at else None,
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
        # The vendor's countersign screen (DECISIONS #34).
        from app.services.countersign_service import consent_view

        out["vendor_esign_consent"] = consent_view(vendor) if vendor else None
    else:
        # The client's signing page (DECISIONS #27).
        out["esign_consent"] = esign_consent.view()
        out["signing_code_required"] = config.SIGNING_REQUIRE_CODE
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


BLANK = "___"


# ── An addendum's priced lines (0081, DECISIONS #38) ─────────────────

MAX_ADDED_LINES = 10


def _changes(kind: str, raw_lines: list | None, removed: list | None, booking: Booking) -> tuple[list | None, list | None]:
    """Validate an addendum's added lines (custom items: a name, a price and a
    quantity) and the contract lines it removes. A cancellation carries none."""
    if not raw_lines and not removed:
        return None, None
    if kind != "addendum":
        raise DocumentServiceError(400, "Only an addendum can change what's sold")
    if raw_lines and len(raw_lines) > MAX_ADDED_LINES:
        raise DocumentServiceError(400, f"An addendum can add at most {MAX_ADDED_LINES} items")
    lines = []
    for r in raw_lines or []:
        name = (r.get("name") or "").strip()[:200]
        price, qty = r.get("unit_price_cents"), r.get("quantity", 1)
        if not name:
            raise DocumentServiceError(400, "Every added item needs a name")
        if not isinstance(price, int) or isinstance(price, bool) or price < 0:
            raise DocumentServiceError(400, f"“{name}” needs a price")
        if not isinstance(qty, (int, float)) or isinstance(qty, bool) or not (0 < qty <= 100_000):
            raise DocumentServiceError(400, f"“{name}” needs a quantity above zero")
        lines.append({"kind": "custom", "name": name, "unit_price_cents": price, "quantity": qty})
    have = {li["id"] for li in booking.line_items or []}
    gone = [str(x) for x in removed or []]
    if any(x not in have for x in gone):
        raise DocumentServiceError(400, "That item isn't on the contract")
    return lines or None, gone or None


def _effect(d: ContractDocument, booking: Booking, db: Session) -> dict | None:
    """The booking as it would be once this addendum is signed: its items,
    total and payment schedule. None for a text-only document."""
    if not d.line_items and not d.removed_line_ids:
        return None
    removed = set(d.removed_line_ids or [])
    kept = [li for li in booking.line_items or [] if li["id"] not in removed]
    try:
        items, primary = doc.normalize_line_items(kept + list(d.line_items or []), vendor_id=booking.vendor_id, db=db)
        _, total = doc.totals(items, booking.discount_cents)
    except doc.DocumentError as e:
        raise DocumentServiceError(400, e.detail)
    before = booking.amount_cents or 0
    schedule = [dict(i) for i in booking.payment_schedule or []]
    delta = total - before
    # The difference lands on what's still to pay, latest first; more than
    # that can't come off, and an addition with nothing left to pay is a
    # new payment due when it's signed.
    open_rows = [i for i in schedule if not i.get("confirmed_at") and not i.get("marked_paid_at")]
    if delta < 0 and -delta > sum(i["amount_cents"] for i in open_rows):
        raise DocumentServiceError(400, "That takes off more than is still to be paid — refund it directly instead")
    if delta > 0 and not open_rows:
        # Due the day the addendum is signed: "on signing" would count from
        # the contract's signature and read as long overdue.
        schedule.append({"label": d.title[:80], "amount_cents": delta, "due_type": "date",
                         "due_date": _now().date().isoformat()})
    else:
        for i in reversed(open_rows):
            if delta == 0:
                break
            take = delta if delta > 0 else max(delta, -(i["amount_cents"] - 1))
            i["amount_cents"] += take
            delta -= take
    return {"items": items, "primary": primary, "total_before": before, "total_after": total, "schedule": schedule}


def changes_view(d: ContractDocument, booking: Booking, db: Session) -> dict | None:
    """An addendum's priced changes in the words its signed copy and PDF use:
    what's added, what's removed, and the total before and after."""
    if not d.line_items and not d.removed_line_ids:
        return None
    names = {li["id"]: li["name"] for li in booking.line_items or []}
    totals = _totals_view(d, booking, db)
    return {
        "added": [
            {"name": li["name"], "quantity": li["quantity"], "unit_price_cents": li["unit_price_cents"],
             "total_cents": round(li["unit_price_cents"] * li["quantity"])}
            for li in d.line_items or []
        ],
        "removed": [names.get(x, "An item") for x in d.removed_line_ids or []],
        "total_before": totals["total_before"],
        "total_after": totals["total_after"],
    }


def apply_addendum(d: ContractDocument, booking: Booking, db: Session) -> None:
    """Both sides signed: the booking's items, total and schedule follow the
    addendum (decision B, 2026-10-26)."""
    effect = _effect(d, booking, db)
    if effect is None:
        return
    booking.line_items, booking.service_id = effect["items"], effect["primary"]
    booking.amount_cents = effect["total_after"]
    if booking.payment_schedule:
        # normalize_schedule drops payment marks (a new contract has none);
        # a signed booking's payments keep theirs.
        marks = {i["id"]: i for i in booking.payment_schedule}
        schedule = doc.normalize_schedule(effect["schedule"], total_cents=effect["total_after"])
        for i in schedule:
            old = marks.get(i["id"], {})
            for key in ("marked_paid_at", "confirmed_at", "paid_via"):
                if old.get(key):
                    i[key] = old[key]
        booking.payment_schedule = schedule
        doc.sync_legacy_payment_fields(booking)
    doc.record(db, booking, "addendum_applied", "system", {
        "document_id": d.document_id, "title": d.title,
        "total_before": effect["total_before"], "total_after": effect["total_after"],
    })


def _deliver(d: ContractDocument, booking: Booking, vendor: Vendor, db: Session, email_client: bool) -> None:
    # The starters leave blanks for what only the vendor knows; a client
    # signed "Payments made so far are handled as follows:" with nothing
    # after it in the 2026-10-26 test. Same rule as a contract's sections.
    unfilled = next((s for s in d.sections or [] if BLANK in f"{s.get('title', '')} {s.get('body', '')}"), None)
    if unfilled is not None:
        raise DocumentServiceError(400, f"Fill in the blank ({BLANK}) in “{unfilled.get('title') or 'a section'}” before sending")
    _effect(d, booking, db)  # the booking may have moved since it was drafted
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
    line_items: list | None = None, removed_line_ids: list | None = None,
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
    d.line_items, d.removed_line_ids = _changes(kind, line_items, removed_line_ids, booking)
    _effect(d, booking, db)  # refuses a change that can't apply
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
    line_items: list | None = None, removed_line_ids: list | None = None, changes_sent: bool = False,
) -> dict:
    d, booking = _own_document(document_id, caller_user_id, db)
    if d.status == "awaiting_vendor":
        raise DocumentServiceError(400, "Your client has signed this, so it can no longer be edited")
    if d.status in ("signed", "declined", "voided"):
        raise DocumentServiceError(400, f"This document was {d.status} and can no longer be edited")
    if title is not None:
        d.title = _title(title, d.kind)
    if sections is not None:
        d.sections = _sections(sections)
    if changes_sent:
        d.line_items, d.removed_line_ids = _changes(d.kind, line_items, removed_line_ids, booking)
        _effect(d, booking, db)
    d.updated_at = _now()
    doc.record(db, booking, "document_edited", "vendor", {"document_id": d.document_id})
    db.commit()
    db.refresh(d)
    return _dict(d, booking, db)


def send_document(*, document_id: str, caller_user_id: str, email_client: bool, db: Session) -> dict:
    d, booking = _own_document(document_id, caller_user_id, db)
    if d.status == "awaiting_vendor":
        raise DocumentServiceError(400, "Your client has signed this, so it can't be sent")
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
    if d.status == "awaiting_vendor":
        raise DocumentServiceError(
            400, "Your client has signed this — sign it, or decline to sign and tell them why",
        )
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
    if d.status in ("signed", "awaiting_vendor"):
        raise DocumentServiceError(400, "This document has already been signed")


def get_by_token(*, token: str, preview: bool, db: Session, client: dict | None = None) -> dict:
    """The couple opening their link. The first open marks it viewed; the
    vendor's own preview doesn't. `client` is where that open came from."""
    d, booking = _by_token(token, db)
    if not preview and d.viewed_at is None and d.status == "sent":
        d.status = "viewed"
        d.viewed_at = _now()
        doc.record(db, booking, "document_viewed", "client", {"document_id": d.document_id, **(client or {})})
        db.commit()
        db.refresh(d)
    return _dict(d, booking, db, with_token=False)


def send_signing_code(*, token: str, db: Session) -> dict:
    """Email the client the code they sign this document with (DECISIONS #27)."""
    d, booking = _by_token(token, db)
    _require_open(d)
    vendor = db.query(Vendor).filter(Vendor.vendor_id == d.vendor_id).first()
    try:
        return signing_evidence.send_code(
            db, booking, email=_client_email(booking, db), vendor_name=_vendor_name(vendor, db) or "your vendor",
            what=f"the {KIND_LABEL.get(d.kind, 'document')} “{d.title}”", document_id=d.document_id,
        )
    except signing_evidence.SigningError as e:
        raise DocumentServiceError(e.status_code, e.detail)


def sign_by_token(
    *, token: str, signer_name: str, db: Session, code: str | None = None, consent: bool = False,
    consent_version: str | None = None, client: dict | None = None,
) -> dict:
    d, booking = _by_token(token, db)
    _require_open(d)
    if not signer_name or not signer_name.strip():
        raise DocumentServiceError(400, "Type your full legal name to sign")
    client_email = _client_email(booking, db)
    try:
        evidence = signing_evidence.collect(
            db, booking, email=client_email, client=client, code=code,
            consent=consent, consent_version=consent_version, document_id=d.document_id,
            signer_name=signer_name, on_contract=_client_name(booking, db),
        )
    except signing_evidence.SigningError as e:
        raise DocumentServiceError(e.status_code, e.detail)
    d.signer_name = signer_name.strip()[:255]
    d.client_signed_at = _now()
    d.status = "awaiting_vendor"
    d.updated_at = d.client_signed_at
    snapshot = {
        "document_id": d.document_id,
        "booking_id": d.booking_id,
        "kind": d.kind,
        "title": d.title,
        "sections": d.sections,
        **({"changes": changes} if (changes := changes_view(d, booking, db)) else {}),
        "date_iso": booking.date_iso,
        "signer_name": d.signer_name,
        "signed_at": d.client_signed_at.isoformat(),
        "evidence": evidence,
    }
    d.signed_snapshot = snapshot
    d.signed_snapshot_sha256 = doc.fingerprint(snapshot)
    doc.record(db, booking, "document_signed", "client", {
        "document_id": d.document_id, "title": d.title, "signer_name": d.signer_name,
        "sha256": d.signed_snapshot_sha256, "ip": evidence["ip"],
        "email_verified": evidence["email_verified_at"] is not None,
        **({"name_on_contract": evidence["name_on_contract"]} if evidence["name_matches"] is False else {}),
    })
    db.commit()
    db.refresh(d)

    from app.services.guest_booking_service import _tell_vendor

    # The signed copy goes to both once the vendor countersigns (DECISIONS #34).
    vendor = db.query(Vendor).filter(Vendor.vendor_id == d.vendor_id).first()
    if client_email:
        send_email(
            to=client_email,
            subject=f"You signed “{d.title}”",
            html=(
                f"<p>You signed the {KIND_LABEL.get(d.kind, 'document')} “{d.title}” with "
                f"{_vendor_name(vendor, db) or 'your vendor'}. They sign next, and we'll email you the signed "
                "copy, with both signatures, as soon as they do.</p>"
            ),
        )
    _tell_vendor(
        booking, f"{d.signer_name} signed “{d.title}” — add your signature",
        f"For the booking on {booking.date_iso}.", "document_client_signed", db,
    )
    return _dict(d, booking, db, with_token=False)


def send_signed_copies(d: ContractDocument, booking: Booking, client_email: str | None, db: Session) -> None:
    """Both sides get the signed PDF (DECISIONS #27), once both have signed
    (#34). Best-effort, after the countersignature is committed; each send
    that went out is on the timeline."""
    from app.services import pdf_service

    try:
        attachment = [pdf_attachment(pdf_service.document_pdf(d, booking, db, _client_name(booking, db)))]
    except Exception:
        logger.exception("Couldn't draw the signed PDF for document %s", d.document_id)
        attachment = None
    vendor = db.query(Vendor).filter(Vendor.vendor_id == d.vendor_id).first()
    vendor_user = db.query(User).filter(User.user_id == vendor.user_id).first() if vendor else None
    vendor_name = _vendor_name(vendor, db) or "your vendor"
    label = KIND_LABEL.get(d.kind, "document")
    for to, who, html in (
        (client_email, "client",
         f"<p>You and {vendor_name} have both signed the {label} “{d.title}”. Your signed copy is "
         "attached.</p>"),
        (getattr(vendor_user, "email", None), "vendor",
         f"<p>You and {d.signer_name} have both signed the {label} “{d.title}”. The signed copy is "
         f"attached, with its signing certificate.</p>"),
    ):
        if not to:
            continue
        sent = send_email(to=to, subject=f"Signed: {d.title}", html=html, attachments=attachment)
        if sent.get("success"):
            doc.record(db, booking, "copy_sent", "system", {
                "to": who, "email_id": sent.get("id"), "document_id": d.document_id,
            })
    db.commit()


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
