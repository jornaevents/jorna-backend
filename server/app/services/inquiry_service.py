"""The public inquiry form: anyone, signed in or not, can send a vendor an
inquiry, and it lands in the vendor's Leads as a new lead (docs/DECISIONS.md
#32).

No caller to check, so the guards are about the vendor and about abuse:
- the vendor must exist and have a package (a vendor still in setup isn't
  taking inquiries);
- a filled-in honeypot field (the hidden "website" input) gets the normal
  reply but nothing is saved or sent;
- at most DAILY_CAP inquiries per vendor per 24 hours (the per-IP limit is
  the router's);
- the confirmation to the person who asked is fixed wording plus the
  vendor's name, never anything they typed, and at most one per address per
  vendor per day, so the form can't be used to send text to any inbox.
"""

import logging
from datetime import datetime, timedelta, timezone
from html import escape

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import WEB_APP_URL
from app.db.models import Lead, Service, User, Vendor
from app.services.contract_service import _EMAIL, ContractError, _check_lead
from app.services.email_service import send_email

logger = logging.getLogger(__name__)

DAILY_CAP = 50


class InquiryError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


def _utcnow() -> datetime:
    # Naive UTC, like the other timestamps on leads.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _open_vendor(vendor_id: str, db: Session) -> tuple[Vendor, User | None]:
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    has_package = vendor is not None and (
        db.query(Service.service_id)
        .filter(Service.vendor_id == vendor_id, Service.status != "archived")
        .first()
        is not None
    )
    if not has_package:
        raise InquiryError(404, "We couldn't find that vendor")
    return vendor, db.query(User).filter(User.user_id == vendor.user_id).first()


def _vendor_name(user: User | None) -> str:
    return f"{user.f_name or ''} {user.l_name or ''}".strip() if user else "the vendor"


def _recent_inquiries(vendor_id: str, since: datetime, db: Session):
    return db.query(Lead).filter(
        Lead.vendor_id == vendor_id, Lead.source == "inquiry_form", Lead.created_at >= since,
    )


def send_inquiry(
    *, vendor_id: str, name: str, email: str, phone: str | None, event_date_iso: str | None,
    event_type: str | None, guest_count: int | None, note: str | None, website: str | None,
    db: Session,
) -> dict:
    _, vendor_user = _open_vendor(vendor_id, db)
    vendor_name = _vendor_name(vendor_user)
    reply = {"received": True, "vendor_name": vendor_name}
    if (website or "").strip():
        logger.info("Inquiry to vendor %s dropped: honeypot filled", vendor_id)
        return reply

    name = (name or "").strip()
    if not name:
        raise InquiryError(422, "Add your name so they know who's asking")
    email = (email or "").strip()
    if not email:
        raise InquiryError(422, "Add your email so they can reply")
    if not _EMAIL.match(email):
        raise InquiryError(422, "That email doesn't look right — check it")
    try:
        checked = _check_lead({
            "email": email, "phone": phone, "event_date_iso": event_date_iso,
            "event_type": event_type, "guest_count": guest_count,
        })
    except ContractError as e:
        raise InquiryError(e.status_code, e.detail)

    now = _utcnow()
    day_ago = now - timedelta(days=1)
    if _recent_inquiries(vendor_id, day_ago, db).count() >= DAILY_CAP:
        raise InquiryError(429, f"{vendor_name} has had a lot of inquiries today — try again tomorrow")
    # Asked this vendor already today? Then they've had their confirmation.
    asked_before = (
        _recent_inquiries(vendor_id, day_ago, db)
        .filter(func.lower(Lead.email) == checked["email"].lower())
        .first()
        is not None
    )

    lead = Lead(
        vendor_id=vendor_id, name=name[:255], email=checked["email"], phone=checked["phone"],
        event_date_iso=checked["event_date_iso"], event_type=checked["event_type"],
        guest_count=checked["guest_count"], note=(note or "").strip() or None,
        status="new", source="inquiry_form", created_at=now, updated_at=now,
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)

    _email_vendor(lead, vendor_user)
    if not asked_before:
        _email_confirmation(lead.email, vendor_name)
    return reply


def _email_vendor(lead: Lead, vendor_user: User | None) -> None:
    """The lead, in full, to the vendor. Replying goes to the person who
    asked. Best effort: the lead is saved either way."""
    if not vendor_user or not vendor_user.email:
        return
    rows = [
        ("Email", lead.email),
        ("Phone", lead.phone),
        ("Event", lead.event_type),
        ("Date", lead.event_date_iso or "Not sure yet"),
        ("Guests", f"About {lead.guest_count:,}" if lead.guest_count else None),
    ]
    details = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;color:#888">{label}</td><td>{escape(str(value))}</td></tr>'
        for label, value in rows if value
    )
    message = (
        f'<p style="white-space:pre-wrap;border-left:3px solid #ddd;padding-left:12px">{escape(lead.note)}</p>'
        if lead.note else ""
    )
    html = (
        f"<p><strong>{escape(lead.name)}</strong> sent you an inquiry from your Jorna page.</p>"
        f"<table>{details}</table>{message}"
        "<p>Reply to this email to answer them, or open it in "
        f'<a href="{WEB_APP_URL}/leads/">your Leads</a> to send a contract.</p>'
    )
    try:
        send_email(
            to=vendor_user.email, subject=f"New inquiry from {lead.name}", html=html, reply_to=lead.email,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Inquiry email to vendor failed for lead %s: %s", lead.lead_id, exc)


def _email_confirmation(to: str, vendor_name: str) -> None:
    """Fixed wording and the vendor's name only — nothing the sender typed."""
    who = escape(vendor_name)
    html = (
        f"<p>Your inquiry reached {who}. They'll reply to this address.</p>"
        "<p>If you didn't send it, you can ignore this email.</p>"
    )
    try:
        send_email(to=to, subject=f"Your message reached {vendor_name}", html=html)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Inquiry confirmation email failed: %s", exc)
