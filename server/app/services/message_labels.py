"""The label a client sees on a chat with one vendor: Inquiry, Negotiation,
Booked or Closed — the same one that vendor sees on the chat in their own
Messages (docs/DECISIONS.md #42).

The vendor web app works its label out itself, from the Leads pipeline and
its bookings (jorna-website, apps/vendor/web/src/lib/messageLabels.ts). The
client app has neither — Leads is vendor-only, and a client's bookings are
spread across their plans — so for the client it's worked out here, with the
pipeline's own stage rule (pipeline_service) and the vendor app's priority
order, so the two sides can't disagree.

Two differences, both on purpose:

- A vendor's own leads (typed in, or added from a Messages thread) aren't
  used. They're the vendor's notes; the client sees a label once they've
  sent a request.
- A request on a plan the client hasn't sent yet isn't used either, because
  the vendor hasn't seen it (booking_service.sent_to_vendor).
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Booking, Conversation, Service
from app.models.schemas import BookingStatus, PaymentStatus
from app.services.booking_service import sent_to_vendor
from app.services.pipeline_service import SENT_STATES, _is_live_unsigned, _outcome

SUBJECT_BOOKING = "booking"
SUBJECT_ENQUIRY = "enquiry"

_AGREED_WITHOUT_CONTRACT = (BookingStatus.APPROVED.value, BookingStatus.PAYMENT_CONFIRMED.value)
_DEAD_STATUSES = ("rejected", "cancelled")


def _is_dead(b: Booking) -> bool:
    """booking_service's is_dead: nothing further can happen to it."""
    return b.status in _DEAD_STATUSES or (b.payment_status or "") == PaymentStatus.REFUNDED.value


def _is_agreed(b: Booking) -> bool:
    """Booked, as the vendor's Bookings page means it (vendorPlan.bookingTab):
    a signed contract, or an approved request from before contracts."""
    if _is_dead(b):
        return False
    if b.contract_token:
        return b.signed_at is not None
    return b.status in _AGREED_WITHOUT_CONTRACT


def _is_over(b: Booking, today: str) -> bool:
    last_day = (b.date_end or b.date_iso or "")[:10]
    return bool(last_day) and last_day < today


def _stage(b: Booking) -> str:
    """pipeline_service._booking_item's stage. The stored contract status is
    enough: "expired" is only ever derived from "sent" or "viewed", and all
    three are sent states."""
    return "negotiation" if b.contract_token and b.contract_status in SENT_STATES else "inquiry"


def _touched(b: Booking) -> datetime:
    """When the request last moved, like a pipeline item's updated_at."""
    stamps = [b.created_at, b.sent_at, b.viewed_at, b.declined_at, b.cancelled_at]
    present = [s.replace(tzinfo=None) for s in stamps if s is not None]
    return max(present) if present else datetime.min


def label_for(conv: Conversation, bookings: list[Booking], today: str) -> tuple[str, Booking] | None:
    """(label, the booking it's about) for one chat, from the client's
    bookings with that chat's vendor. The order is the vendor app's
    relationFor:

    1. The chat's own booking, once agreed (Booked).
    2. Another agreed booking that hasn't happened yet (Booked), soonest
       first — a question whose request was later signed.
    3. An open request (Inquiry or Negotiation): the chat's own first, then
       Negotiation before Inquiry, then the most recently touched. A new
       request after a past event isn't hidden behind the old booking.
    4. A past agreed booking (Booked), the latest.
    5. A request that ended — the vendor declined, the client cancelled,
       or the client declined the contract — or a booking called off (Closed).
    """
    own_id = conv.booking_id
    agreed = [b for b in bookings if _is_agreed(b)]
    own = next((b for b in agreed if b.booking_id == own_id), None)
    if own:
        return "booked", own

    upcoming = [b for b in agreed if not _is_over(b, today)]
    if upcoming:
        return "booked", min(upcoming, key=lambda b: b.date_iso or "")

    def rank(b: Booking) -> tuple:
        return (
            0 if own_id is not None and b.booking_id == own_id else 1,
            0 if _stage(b) == "negotiation" else 1,
        )

    open_ = [b for b in bookings if _is_live_unsigned(b)]
    if open_:
        best = min(sorted(open_, key=_touched, reverse=True), key=rank)
        return _stage(best), best

    if agreed:
        return "booked", max(agreed, key=lambda b: b.date_iso or "")

    ended = [b for b in bookings if not _is_live_unsigned(b) and _outcome(b)]
    if ended:
        return "closed", min(sorted(ended, key=_touched, reverse=True), key=rank)
    called_off = [b for b in bookings if _is_dead(b)]
    if called_off:
        return "closed", min(called_off, key=lambda b: 0 if b.booking_id == own_id else 1)
    return None


def _client_of(conv: Conversation, booking_owner: dict[str, str | None]) -> str | None:
    if conv.client_user_id:
        return conv.client_user_id
    # A booking thread from before client_user_id was recorded.
    return booking_owner.get(conv.booking_id) if conv.booking_id else None


def client_labels(conversations: list[Conversation], caller_user_id: str, db: Session) -> dict[str, dict]:
    """conversation_id → {"stage", "booking_id", "service_name"} for each
    two-person chat in which the caller is the client and there's something
    to label. Chats with nothing to label, plan chats, and every chat read by
    its vendor are left out. Two queries for the whole inbox."""
    direct = [
        c for c in conversations
        if c.subject_type in (SUBJECT_BOOKING, SUBJECT_ENQUIRY) and c.vendor_id
    ]
    if not direct:
        return {}
    own_ids = [c.booking_id for c in direct if c.booking_id and not c.client_user_id]
    owners = {
        b.booking_id: b.user_id
        for b in db.query(Booking).filter(Booking.booking_id.in_(own_ids)).all()
    } if own_ids else {}
    mine = [c for c in direct if _client_of(c, owners) == caller_user_id]
    if not mine:
        return {}

    vendor_ids = {c.vendor_id for c in mine}
    rows = sent_to_vendor(
        db.query(Booking, Service.name)
        .outerjoin(Service, Service.service_id == Booking.service_id)
        .filter(Booking.user_id == caller_user_id, Booking.vendor_id.in_(vendor_ids))
    ).all()
    by_vendor: dict[str, list[Booking]] = {}
    service_name: dict[str, str | None] = {}
    for booking, name in rows:
        by_vendor.setdefault(booking.vendor_id, []).append(booking)
        service_name[booking.booking_id] = name

    today = datetime.now(timezone.utc).date().isoformat()
    out: dict[str, dict] = {}
    for conv in mine:
        found = label_for(conv, by_vendor.get(conv.vendor_id, []), today)
        if found:
            stage, booking = found
            out[conv.conversation_id] = {
                "stage": stage,
                "booking_id": booking.booking_id,
                "service_name": service_name.get(booking.booking_id),
            }
    return out
