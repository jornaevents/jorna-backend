"""A vendor's leads: everything before a contract is signed, in one list.

The web app's Leads page (redesign step 2, docs/DECISIONS.md #20) used to be
assembled from three places — pending marketplace requests, unsigned
contracts, and the informal Lead table — each with its own idea of what
"needs you" meant. This puts them in one list with one set of rules, so web
and iOS can't disagree:

- **Inquiry**: nothing sent yet. A marketplace request, a lead (typed in, or
  added from a Messages thread), or a contract still in draft.
- **Negotiation**: the contract link has gone to the couple — sent, viewed,
  countered, declined or expired — and it isn't signed.

Signed contracts are bookings, not leads, and leave this list. Dead ones
(declined requests, voided contracts, converted or lost leads) leave it too.
Archived items stay in it, flagged, so the page can show them on request.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Booking, Conversation, ConversationMember, Lead, User, Vendor
from app.models.schemas import BookingStatus
from app.services.booking_service import _booking_dict
from app.utils.timeutil import utc_iso


class PipelineError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


SUBJECT_ENQUIRY = "enquiry"

# Contract states once the link has been sent. "expired" is derived on read
# (contract_service.contract_state) but arrives here already resolved via
# _booking_dict's contract_status.
SENT_STATES = ("sent", "viewed", "declined", "expired")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _vendor_for(caller_user_id: str, db: Session) -> Vendor:
    vendor = db.query(Vendor).filter(Vendor.user_id == caller_user_id).first()
    if not vendor:
        raise PipelineError(403, "You must be a vendor to do this")
    return vendor


def _is_live_unsigned(b: Booking) -> bool:
    if b.signed_at is not None or b.cancelled_at is not None:
        return False
    if b.status == BookingStatus.REJECTED.value:
        return False
    if b.contract_token:
        return b.contract_status != "voided"
    # A marketplace request before it became a proposal.
    return b.status in (BookingStatus.PENDING.value, BookingStatus.NEGOTIATION_ONGOING.value)


def _attention(source: str, stage: str, d: dict) -> tuple[str | None, str | None]:
    """(needs_you | waiting | None, why). The order is the order a vendor
    should act in: a couple's counter beats the contract's own state."""
    if d.get("negotiation_awaiting_role") == "vendor":
        return "needs_you", "counter_offer"
    if d.get("negotiation_awaiting_role") == "client":
        return "waiting", "counter_sent"
    if source == "request":
        return "needs_you", "new_request"
    status = d.get("contract_status")
    if status == "declined":
        return "needs_you", "declined"
    if status == "expired":
        return "needs_you", "expired"
    if status == "draft":
        return "needs_you", "draft"
    if status in ("sent", "viewed"):
        return "waiting", status
    return None, None


def _latest(*stamps: str | None) -> str | None:
    present = [s for s in stamps if s]
    return max(present) if present else None


def _booking_item(b: Booking, d: dict, conversation_id: str | None) -> dict:
    source = "contract" if b.contract_token else "request"
    stage = "negotiation" if b.contract_token and d.get("contract_status") in SENT_STATES else "inquiry"
    archived = b.vendor_archived_at is not None
    attention, reason = (None, None) if archived else _attention(source, stage, d)
    price = d.get("price")
    return {
        "id": f"booking:{b.booking_id}",
        "source": source,
        "stage": stage,
        "booking_id": b.booking_id,
        "lead_id": None,
        "conversation_id": conversation_id,
        "name": d.get("guest_name") or d.get("client_name") or "Client",
        "email": d.get("guest_email"),
        "phone": d.get("guest_phone"),
        "event_name": d.get("event_name"),
        "event_date": d.get("date_iso"),
        "event_date_end": d.get("date_end"),
        "location": d.get("location"),
        "service_name": d.get("service_name"),
        "estimated_value_cents": round(price * 100) if price is not None else None,
        "contract_status": d.get("contract_status"),
        "hold_expires_at": d.get("hold_expires_at"),
        "attention": attention,
        "attention_reason": reason,
        "archived": archived,
        "created_at": d.get("created_at"),
        "updated_at": _latest(
            d.get("created_at"), d.get("sent_at"), d.get("viewed_at"), d.get("declined_at"),
        ),
    }


def _lead_item(lead: Lead) -> dict:
    archived = lead.archived_at is not None
    # A lead nobody has replied to yet is the vendor's move; once they've
    # marked it contacted or quoted, it's waiting on the couple.
    if archived:
        attention, reason = None, None
    elif lead.status == "new":
        attention, reason = "needs_you", "new_lead"
    else:
        attention, reason = "waiting", lead.status
    return {
        "id": f"lead:{lead.lead_id}",
        "source": "lead",
        "stage": "inquiry",
        "booking_id": None,
        "lead_id": lead.lead_id,
        "conversation_id": lead.conversation_id,
        "name": lead.name,
        "email": lead.email,
        "phone": lead.phone,
        "event_name": None,
        "event_date": lead.event_date_iso,
        "event_date_end": None,
        "location": None,
        "service_name": None,
        "estimated_value_cents": None,
        "contract_status": None,
        "hold_expires_at": None,
        "attention": attention,
        "attention_reason": reason,
        "archived": archived,
        "created_at": utc_iso(lead.created_at),
        "updated_at": utc_iso(lead.updated_at),
        "note": lead.note,
        "lead_status": lead.status,
    }


def _conversations_for(vendor: Vendor, bookings: list[Booking], leads: list[Lead], db: Session) -> dict[str, str]:
    """booking_id → the thread a vendor would open about it: the booking's own
    thread, else the couple's enquiry with this vendor, else the thread the
    lead it came from was added from. Three queries, not one per row."""
    ids = [b.booking_id for b in bookings]
    out: dict[str, str] = {}
    if not ids:
        return out
    for c in db.query(Conversation).filter(Conversation.booking_id.in_(ids)).all():
        out[c.booking_id] = c.conversation_id
    clients = {b.user_id for b in bookings if b.user_id}
    if clients:
        enquiries = {
            c.client_user_id: c.conversation_id
            for c in db.query(Conversation).filter(
                Conversation.subject_type == SUBJECT_ENQUIRY,
                Conversation.vendor_id == vendor.vendor_id,
                Conversation.client_user_id.in_(clients),
            ).all()
        }
        for b in bookings:
            if b.booking_id not in out and b.user_id in enquiries:
                out[b.booking_id] = enquiries[b.user_id]
    for lead in leads:
        if lead.converted_booking_id and lead.conversation_id and lead.converted_booking_id not in out:
            out[lead.converted_booking_id] = lead.conversation_id
    return out


def list_pipeline(*, caller_user_id: str, db: Session) -> dict:
    vendor = _vendor_for(caller_user_id, db)
    bookings = [
        b for b in db.query(Booking).filter(Booking.vendor_id == vendor.vendor_id).all()
        if _is_live_unsigned(b)
    ]
    all_leads = db.query(Lead).filter(Lead.vendor_id == vendor.vendor_id).all()
    conversations = _conversations_for(vendor, bookings, all_leads, db)

    items = [_booking_item(b, _booking_dict(b, db), conversations.get(b.booking_id)) for b in bookings]
    items += [
        _lead_item(lead) for lead in all_leads
        if not lead.converted_booking_id and lead.status not in ("won", "lost")
    ]
    items.sort(key=lambda i: i["updated_at"] or "", reverse=True)

    active = [i for i in items if not i["archived"]]
    return {
        "items": items,
        "counts": {
            "inquiries": sum(1 for i in active if i["stage"] == "inquiry"),
            "negotiations": sum(1 for i in active if i["stage"] == "negotiation"),
            "needs_you": sum(1 for i in active if i["attention"] == "needs_you"),
            "waiting": sum(1 for i in active if i["attention"] == "waiting"),
            "archived": len(items) - len(active),
        },
    }


def set_booking_archived(*, booking_id: str, archived: bool, caller_user_id: str, db: Session) -> dict:
    """Hide a request or unsigned contract from the vendor's active leads, or
    bring it back. Doesn't decline, void or notify anyone — a signed booking
    can't be archived, since it's no longer a lead."""
    vendor = _vendor_for(caller_user_id, db)
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking or booking.vendor_id != vendor.vendor_id:
        raise PipelineError(404, "Booking not found")
    if booking.signed_at is not None:
        raise PipelineError(400, "A signed booking isn't a lead and can't be archived")
    booking.vendor_archived_at = _now() if archived else None
    db.commit()
    db.refresh(booking)
    return _booking_dict(booking, db)


def lead_from_conversation(*, conversation_id: str, caller_user_id: str, db: Session) -> tuple[dict, bool]:
    """"Add to leads" from a Messages thread: a lead for the couple on the
    other side. Returns (lead, created). Asking twice returns the same open
    lead instead of a duplicate."""
    from app.services.contract_service import _lead_dict

    vendor = _vendor_for(caller_user_id, db)
    conv = db.query(Conversation).filter(Conversation.conversation_id == conversation_id).first()
    is_member = conv and db.query(ConversationMember).filter(
        ConversationMember.conversation_id == conversation_id,
        ConversationMember.user_id == caller_user_id,
    ).first()
    if not conv or not is_member:
        raise PipelineError(404, "Conversation not found")

    client_id = conv.client_user_id
    if not client_id:
        # A bundle chat has no single couple to make a lead of.
        raise PipelineError(400, "Only a conversation with one couple can become a lead")

    existing = db.query(Lead).filter(
        Lead.vendor_id == vendor.vendor_id,
        Lead.conversation_id == conversation_id,
        Lead.converted_booking_id.is_(None),
        Lead.status.notin_(("won", "lost")),
    ).first()
    if existing:
        if existing.archived_at is not None:
            existing.archived_at = None
            existing.updated_at = _now()
            db.commit()
            db.refresh(existing)
        return _lead_dict(existing), False

    client = db.query(User).filter(User.user_id == client_id).first()
    name = f"{client.f_name or ''} {client.l_name or ''}".strip() if client else ""
    now = _now()
    lead = Lead(
        vendor_id=vendor.vendor_id,
        name=name or "New couple",
        email=client.email if client else None,
        phone=client.phone if client else None,
        status="new",
        user_id=client_id,
        conversation_id=conversation_id,
        created_at=now,
        updated_at=now,
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return _lead_dict(lead), True
