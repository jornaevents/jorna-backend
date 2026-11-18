"""The inquiry form: a signed-in client with a confirmed email sends a
vendor an inquiry. It opens (or reuses) their message thread with the vendor
and lands in the vendor's Leads (docs/DECISIONS.md #32, #45).

The guards:
- the vendor must exist and have a package (a vendor still in setup isn't
  taking inquiries);
- the sender must be a client whose email is confirmed (#44), never a vendor
  account;
- a filled-in honeypot field (the hidden "website" input) gets the normal
  reply but nothing is saved or sent;
- a package and an event date are required (#47), and so are what the
  event is and where, plus a guest count when the package is priced per
  person (2026-11-16, #52);
- at most DAILY_CAP inquiries per vendor per 24 hours, and the thread limits
  every first question to a vendor has (conversation_service);
- the confirmation repeats what they sent, safe now that the address is
  proved, and goes at most once per client per vendor per day.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from html import escape

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import CLIENT_WEB_APP_URL, WEB_APP_URL
from app.db.models import GroupMessage, Lead, Service, User, Vendor
from app.services.booking_service import _normalize_unit
from app.services.plan_readiness import _ZIP, US_STATE_CODES
from app.services.contract_service import ContractError, _check_lead
from app.services.conversation_service import ConversationError, open_enquiry
from app.services.email_service import send_email
from app.services.names import vendor_user_display_name

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
    return vendor_user_display_name(user, "the vendor")


def _recent_inquiries(vendor_id: str, since: datetime, db: Session):
    return db.query(Lead).filter(
        Lead.vendor_id == vendor_id, Lead.source == "inquiry_form", Lead.created_at >= since,
    )


def _date_label(iso: str | None) -> str | None:
    if not iso:
        return None
    day = date.fromisoformat(iso)
    return f"{day:%b} {day.day}, {day.year}"


def _clock(hhmm: str) -> str:
    """"16:30" → "4:30 PM"."""
    h, m = (int(x) for x in hhmm.split(":"))
    return f"{h % 12 or 12}:{m:02d} {'PM' if h >= 12 else 'AM'}"


def _times_label(start: str | None, end: str | None) -> str | None:
    if start and end:
        return f"{_clock(start)} – {_clock(end)}"
    return None


def _details_line(checked: dict, service: Service) -> str:
    """"Sangeet · Mar 12, 2027 · Edison, NJ · about 180 guests · Sangeet
    coverage" — what they filled in."""
    parts = [
        checked["event_type"],
        _date_label(checked["event_date_iso"]),
        _times_label(checked.get("time_start"), checked.get("time_end")),
        checked["event_location"],
        f"about {checked['guest_count']:,} guests" if checked["guest_count"] else None,
        service.name,
    ]
    return " · ".join(p for p in parts if p)


def _package(vendor_id: str, service_id: str | None, db: Session) -> Service:
    """The package they're asking about: required, and one of this vendor's
    that isn't archived (DECISIONS #47)."""
    if not service_id:
        raise InquiryError(422, "Pick the package you're asking about")
    service = db.query(Service).filter(
        Service.service_id == service_id, Service.vendor_id == vendor_id, Service.status != "archived",
    ).first()
    if service is None:
        raise InquiryError(422, "That package isn't available — pick another")
    return service


def _thread_message(checked: dict, service: Service, note: str | None) -> str:
    """The inquiry as the first (or next) message in their thread: what they
    asked about, then the rest as a list, then their note. The app draws it
    from the message's meta; this text is what older apps and previews show."""
    rows = [
        ("Event", checked["event_type"]),
        ("Date", _date_label(checked["event_date_iso"])),
        ("Time", _times_label(checked.get("time_start"), checked.get("time_end"))),
        ("Where", checked["event_location"]),
        ("Guests", f"About {checked['guest_count']:,}" if checked["guest_count"] else None),
    ]
    lines = [f"Inquiry: {service.name}"] + [f"• {label}: {value}" for label, value in rows if value]
    return "\n\n".join(p for p in ("\n".join(lines), note) if p)


def _location(
    event_location: str | None, kind: str | None, street: str | None, city: str | None,
    state: str | None, zip_code: str | None,
) -> str:
    """Where the event is, required (#52). The form sends it in parts — a
    city and state, or a venue's full address — and it's kept as the one line
    the lead and the contract already use. An older page's single line is
    still accepted."""
    if kind is None:
        line = (event_location or "").strip()
        if not line:
            raise InquiryError(422, "Add where the event is")
        return line
    city = (city or "").strip()
    st = (state or "").strip().upper()
    if not city or st not in US_STATE_CODES:
        raise InquiryError(422, "Add the event's city and state")
    if kind == "city":
        return f"{city}, {st}"
    if kind != "venue":
        raise InquiryError(422, "Choose a city or a venue")
    street = (street or "").strip()
    zip_code = (zip_code or "").strip()
    if not street:
        raise InquiryError(422, "Add the venue's street address")
    if not _ZIP.match(zip_code):
        raise InquiryError(422, "Add the venue's ZIP code")
    return f"{street}, {city}, {st} {zip_code}"


def _inquiry_meta(lead: Lead, checked: dict, service: Service, note: str | None) -> dict:
    """What the chat card shows, and the lead it opens."""
    return {
        "lead_id": lead.lead_id,
        "service_id": service.service_id,
        "service_name": service.name,
        "event_type": checked["event_type"],
        "event_date_iso": checked["event_date_iso"],
        "event_location": checked["event_location"],
        "guest_count": checked["guest_count"],
        "time_start": checked.get("time_start"),
        "time_end": checked.get("time_end"),
        "note": note,
    }


def send_inquiry(
    *, vendor_id: str, caller: User, phone: str | None, event_date_iso: str | None,
    event_type: str | None, guest_count: int | None, note: str | None, website: str | None,
    db: Session, event_location: str | None = None, service_id: str | None = None,
    location_kind: str | None = None, location_street: str | None = None, location_city: str | None = None,
    location_state: str | None = None, location_zip: str | None = None,
    time_start: str | None = None, time_end: str | None = None,
) -> dict:
    vendor, vendor_user = _open_vendor(vendor_id, db)
    vendor_name = _vendor_name(vendor_user)
    if db.query(Vendor.vendor_id).filter(Vendor.user_id == caller.user_id).first():
        raise InquiryError(403, "Switch to a client account to send inquiries")
    if caller.email_verified_at is None:
        raise InquiryError(403, "Confirm your email before sending an inquiry")
    reply = {"received": True, "vendor_name": vendor_name, "conversation_id": None}
    if (website or "").strip():
        logger.info("Inquiry to vendor %s dropped: honeypot filled", vendor_id)
        return reply

    name = f"{caller.f_name or ''} {caller.l_name or ''}".strip() or caller.email
    if not (event_date_iso or "").strip():
        raise InquiryError(422, "Add your event date")
    if not (event_type or "").strip():
        raise InquiryError(422, "Add what the event is")
    event_location = _location(
        event_location, location_kind, location_street, location_city, location_state, location_zip,
    )
    try:
        checked = _check_lead({
            "email": caller.email, "phone": phone or caller.phone, "event_date_iso": event_date_iso,
            "event_type": event_type, "event_location": event_location, "guest_count": guest_count,
        })
    except ContractError as e:
        raise InquiryError(e.status_code, e.detail)
    note = (note or "").strip() or None
    # Both or neither: half a time range is a contract that can't be signed.
    if bool(time_start) != bool(time_end):
        raise InquiryError(422, "Add both a start and an end time, or leave both out")
    checked["time_start"], checked["time_end"] = time_start or None, time_end or None
    service = _package(vendor_id, service_id, db)
    if _normalize_unit(service.price_unit) == "person" and not checked["guest_count"]:
        raise InquiryError(422, f"Add a guest count — {service.name} is priced per guest")

    now = _utcnow()
    day_ago = now - timedelta(days=1)
    if _recent_inquiries(vendor_id, day_ago, db).count() >= DAILY_CAP:
        raise InquiryError(429, f"{vendor_name} has had a lot of inquiries today — try again tomorrow")
    # Asked this vendor already today? Then they've had their confirmation.
    asked_before = (
        db.query(Lead.lead_id)
        .filter(
            Lead.vendor_id == vendor_id, Lead.user_id == caller.user_id,
            Lead.source == "inquiry_form", Lead.updated_at >= day_ago,
        )
        .first()
        is not None
    )

    details = _details_line(checked, service)
    try:
        thread = open_enquiry(
            vendor_id=vendor_id, content=_thread_message(checked, service, note),
            caller_user_id=caller.user_id, db=db,
            # The package rides along as the message's reference card.
            service_id=service.service_id,
        )
    except ConversationError as e:
        raise InquiryError(e.status_code, e.detail)

    lead = _lead_for(vendor.vendor_id, caller, thread["conversation_id"], checked, service, name, note, now, db)
    # The card needs the lead, and the lead needs the thread: so the message
    # is posted first and told which lead it is afterwards.
    message = thread["message"]
    row = db.query(GroupMessage).filter(GroupMessage.message_id == message["message_id"]).first()
    if row is not None:
        row.meta = {**(row.meta or {}), "inquiry": _inquiry_meta(lead, checked, service, note)}
        db.commit()
        message = {**message, "meta": row.meta}
    _email_vendor(lead, vendor_user, thread["conversation_id"])
    if not asked_before:
        _email_confirmation(caller.email, vendor_name, details, note, thread["conversation_id"])
    return {**reply, "conversation_id": thread["conversation_id"], "message": message}


def _lead_for(
    vendor_id: str, caller: User, conversation_id: str, checked: dict, service: Service,
    name: str, note: str | None, now: datetime, db: Session,
) -> Lead:
    """Their open lead on this thread, brought up to date and back to "needs
    you" — a second inquiry is the same client asking again, not a new one —
    or a new lead."""
    lead = db.query(Lead).filter(
        Lead.vendor_id == vendor_id,
        Lead.conversation_id == conversation_id,
        Lead.converted_booking_id.is_(None),
        Lead.status.notin_(("won", "lost")),
    ).first()
    if lead is None:
        # A lead the vendor typed in for this client, with no chat yet —
        # often the one they emailed their inquiry link to (DECISIONS #56).
        # Matched by the address it was sent to, so one client is one lead.
        lead = db.query(Lead).filter(
            Lead.vendor_id == vendor_id,
            Lead.conversation_id.is_(None),
            Lead.user_id.is_(None),
            func.lower(Lead.email) == (caller.email or "").lower(),
            Lead.converted_booking_id.is_(None),
            Lead.status.notin_(("won", "lost")),
        ).first()
        if lead:
            lead.conversation_id = conversation_id
            lead.user_id = caller.user_id
    if lead:
        for field in ("phone", "event_date_iso", "event_type", "event_location", "guest_count", "time_start", "time_end"):
            if checked[field] is not None:
                setattr(lead, field, checked[field])
        lead.service_id = service.service_id
        if note:
            lead.note = note
        lead.status = "new"
        lead.source = "inquiry_form"
        lead.archived_at = None
        lead.updated_at = now
    else:
        lead = Lead(
            vendor_id=vendor_id, name=name[:255], email=checked["email"], phone=checked["phone"],
            event_date_iso=checked["event_date_iso"], event_type=checked["event_type"],
            guest_count=checked["guest_count"], event_location=checked["event_location"],
            service_id=service.service_id, note=note, status="new", source="inquiry_form",
            time_start=checked["time_start"], time_end=checked["time_end"],
            user_id=caller.user_id, conversation_id=conversation_id, created_at=now, updated_at=now,
        )
        db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


def _email_vendor(lead: Lead, vendor_user: User | None, conversation_id: str) -> None:
    """The lead, in full, to the vendor. Replying by email goes to the client;
    the thread is in Messages too. Best effort: the lead is saved either way."""
    if not vendor_user or not vendor_user.email:
        return
    rows = [
        ("Email", lead.email),
        ("Phone", lead.phone),
        ("Event", lead.event_type),
        ("Date", _date_label(lead.event_date_iso)),
        ("Where", lead.event_location),
        ("Guests", f"About {lead.guest_count:,}" if lead.guest_count else None),
        ("Time", _times_label(lead.time_start, lead.time_end)),
        ("Package", lead.service.name if lead.service else None),
    ]
    details = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;color:#888">{label}</td><td>{escape(str(value))}</td></tr>'
        for label, value in rows if value
    )
    message = (
        f'<p style="white-space:pre-wrap;border-left:3px solid #ddd;padding-left:12px">{escape(lead.note)}</p>'
        if lead.note else ""
    )
    base = WEB_APP_URL.rstrip("/")
    html = (
        f"<p><strong>{escape(lead.name)}</strong> sent you an inquiry from your Jorna page.</p>"
        f"<table>{details}</table>{message}"
        f'<p><a href="{base}/conversation/?id={conversation_id}">Reply in Messages</a>, reply to this email, '
        f'or open it in <a href="{base}/leads/">your Leads</a> to send a contract.</p>'
    )
    try:
        send_email(
            to=vendor_user.email, subject=f"New inquiry from {lead.name}", html=html, reply_to=lead.email,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Inquiry email to vendor failed for lead %s: %s", lead.lead_id, exc)


def _email_confirmation(to: str, vendor_name: str, details: str, note: str | None, conversation_id: str) -> None:
    """A copy of what they sent. Their own words are safe to repeat: the
    address is the account's, and the account has proved it reads it (#44)."""
    sent = "".join(
        f'<p style="white-space:pre-wrap;border-left:3px solid #ddd;padding-left:12px">{escape(part)}</p>'
        for part in (details, note) if part
    )
    html = (
        f"<p>Your inquiry reached {escape(vendor_name)}. Here's what you sent:</p>{sent}"
        f'<p>Their reply will be in <a href="{CLIENT_WEB_APP_URL.rstrip("/")}/conversation/?id={conversation_id}">'
        "your messages on Jorna</a>, and we'll email you when it comes.</p>"
    )
    try:
        send_email(to=to, subject=f"Your inquiry to {vendor_name}", html=html)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Inquiry confirmation email failed: %s", exc)
