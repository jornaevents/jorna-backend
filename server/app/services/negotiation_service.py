"""Business logic for price negotiation on bookings."""

import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.db.models import Booking, Negotiation, NegotiationOffer, Service, User, Vendor

logger = logging.getLogger(__name__)


class NegotiationError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ── Helpers ───────────────────────────────────────────────────────────


def _get_parties(booking: Booking, db: Session) -> tuple[str, str]:
    """Return (client_user_id, vendor_user_id) for a booking."""
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    if not vendor:
        raise NegotiationError(404, "Vendor not found for this booking")
    return booking.user_id, vendor.user_id


def _assert_is_party(booking: Booking, caller_user_id: str, db: Session) -> str:
    """Raise 403 if caller is not a party. Returns the other party's user_id."""
    client_id, vendor_user_id = _get_parties(booking, db)
    if caller_user_id == client_id:
        return vendor_user_id
    if caller_user_id == vendor_user_id:
        return client_id
    raise NegotiationError(403, "You are not a party to this booking")


def _negotiation_dict(neg: Negotiation, offers: list[NegotiationOffer], db: Session) -> dict:
    user_ids = {o.proposed_by for o in offers} | {neg.proposed_by}
    users = {u.user_id: u for u in db.query(User).filter(User.user_id.in_(user_ids)).all()}

    def _user_name(uid: str) -> str:
        u = users.get(uid)
        return f"{u.f_name} {u.l_name}" if u else uid

    return {
        "negotiation_id": neg.negotiation_id,
        "booking_id": neg.booking_id,
        "status": neg.status,
        "current_offer_cents": neg.current_offer_cents,
        "current_offer_dollars": round(neg.current_offer_cents / 100, 2),
        "proposed_by": neg.proposed_by,
        "proposed_by_name": _user_name(neg.proposed_by),
        "created_at": neg.created_at.isoformat(),
        "updated_at": neg.updated_at.isoformat(),
        "offers": [
            {
                "offer_id": o.offer_id,
                "action": o.action,
                "amount_cents": o.amount_cents,
                "amount_dollars": round(o.amount_cents / 100, 2) if o.amount_cents else None,
                "message": o.message,
                "proposed_by": o.proposed_by,
                "proposed_by_name": _user_name(o.proposed_by),
                "created_at": o.created_at.isoformat(),
            }
            for o in offers
        ],
    }


def _notify(receiver_id: str, title: str, body: str, data: dict, db: Session) -> None:
    """Fire a push notification to the other party (all devices) — best effort."""
    try:
        receiver = db.query(User).filter(User.user_id == receiver_id).first()
        if receiver:
            from app.utils.notifications import send_push_to_user
            send_push_to_user(receiver, title, body, data, db=db)
    except Exception as exc:
        logger.warning("Negotiation notification failed: %s", exc)


# ── Service functions ─────────────────────────────────────────────────


def start_negotiation(
    *, booking_id: str, amount_cents: int, message: str | None, caller_user_id: str, db: Session
) -> dict:
    """Open a price negotiation on a booking. Either party can initiate."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise NegotiationError(404, "Booking not found")
    if booking.status not in ("pending", "approved"):
        raise NegotiationError(400, f"Cannot negotiate on a booking with status '{booking.status}'")
    if booking.payment_status not in ("unpaid",):
        raise NegotiationError(400, "Cannot negotiate after payment has been initiated")

    # Negotiation is a per-service toggle the vendor sets; enforce it server-side
    # (previously the flag only gated the client UI and was never checked here).
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    if not service or not service.negotiable:
        raise NegotiationError(400, "This service isn't open to price negotiation.")

    other_party_id = _assert_is_party(booking, caller_user_id, db)

    existing = db.query(Negotiation).filter(Negotiation.booking_id == booking_id).first()
    if existing:
        raise NegotiationError(400, "A negotiation already exists for this booking. Use the offer endpoint to counter.")

    if amount_cents <= 0:
        raise NegotiationError(400, "Offer amount must be greater than zero")

    now = datetime.now(timezone.utc)
    neg = Negotiation(
        booking_id=booking_id,
        status="open",
        current_offer_cents=amount_cents,
        proposed_by=caller_user_id,
        created_at=now,
        updated_at=now,
    )
    db.add(neg)
    db.flush()

    offer = NegotiationOffer(
        negotiation_id=neg.negotiation_id,
        proposed_by=caller_user_id,
        action="offer",
        amount_cents=amount_cents,
        message=message,
        created_at=now,
    )
    db.add(offer)

    # Flip booking into negotiation_ongoing so both parties know the price is under discussion.
    booking.status = "negotiation_ongoing"

    db.commit()
    db.refresh(neg)

    caller = db.query(User).filter(User.user_id == caller_user_id).first()
    caller_name = f"{caller.f_name} {caller.l_name}" if caller else "Someone"
    _notify(
        other_party_id,
        title="New Price Offer",
        body=f"{caller_name} offered ${amount_cents / 100:.2f} for your booking.",
        data={"booking_id": booking_id, "negotiation_id": neg.negotiation_id, "type": "negotiation_offer"},
        db=db,
    )

    return _negotiation_dict(neg, [offer], db)


def get_negotiation(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    """Return the negotiation and full offer history for a booking."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise NegotiationError(404, "Booking not found")
    _assert_is_party(booking, caller_user_id, db)

    neg = db.query(Negotiation).filter(Negotiation.booking_id == booking_id).first()
    if not neg:
        raise NegotiationError(404, "No negotiation found for this booking")

    offers = (
        db.query(NegotiationOffer)
        .filter(NegotiationOffer.negotiation_id == neg.negotiation_id)
        .order_by(NegotiationOffer.created_at.asc())
        .all()
    )
    return _negotiation_dict(neg, offers, db)


def make_offer(
    *, negotiation_id: str, amount_cents: int, message: str | None, caller_user_id: str, db: Session
) -> dict:
    """Counter with a new price. Only the party who did NOT make the last offer can counter."""
    neg = db.query(Negotiation).filter(Negotiation.negotiation_id == negotiation_id).first()
    if not neg:
        raise NegotiationError(404, "Negotiation not found")
    if neg.status != "open":
        raise NegotiationError(400, f"Negotiation is already '{neg.status}'")

    booking = db.query(Booking).filter(Booking.booking_id == neg.booking_id).first()
    other_party_id = _assert_is_party(booking, caller_user_id, db)

    if neg.proposed_by == caller_user_id:
        raise NegotiationError(400, "It's the other party's turn to respond to your offer")
    if amount_cents <= 0:
        raise NegotiationError(400, "Offer amount must be greater than zero")

    now = datetime.now(timezone.utc)
    neg.current_offer_cents = amount_cents
    neg.proposed_by = caller_user_id
    neg.updated_at = now

    offer = NegotiationOffer(
        negotiation_id=negotiation_id,
        proposed_by=caller_user_id,
        action="counter",
        amount_cents=amount_cents,
        message=message,
        created_at=now,
    )
    db.add(offer)
    db.commit()

    offers = (
        db.query(NegotiationOffer)
        .filter(NegotiationOffer.negotiation_id == negotiation_id)
        .order_by(NegotiationOffer.created_at.asc())
        .all()
    )

    caller = db.query(User).filter(User.user_id == caller_user_id).first()
    caller_name = f"{caller.f_name} {caller.l_name}" if caller else "Someone"
    _notify(
        other_party_id,
        title="Counter Offer",
        body=f"{caller_name} countered with ${amount_cents / 100:.2f}.",
        data={"booking_id": neg.booking_id, "negotiation_id": negotiation_id, "type": "negotiation_counter"},
        db=db,
    )

    return _negotiation_dict(neg, offers, db)


def accept_offer(*, negotiation_id: str, caller_user_id: str, db: Session) -> dict:
    """Accept the current offer. Updates booking.amount_cents to the agreed price."""
    neg = db.query(Negotiation).filter(Negotiation.negotiation_id == negotiation_id).first()
    if not neg:
        raise NegotiationError(404, "Negotiation not found")
    if neg.status != "open":
        raise NegotiationError(400, f"Negotiation is already '{neg.status}'")

    booking = db.query(Booking).filter(Booking.booking_id == neg.booking_id).first()
    other_party_id = _assert_is_party(booking, caller_user_id, db)

    if neg.proposed_by == caller_user_id:
        raise NegotiationError(400, "You cannot accept your own offer")

    now = datetime.now(timezone.utc)
    neg.status = "accepted"
    neg.updated_at = now

    # Lock in the agreed price on the booking and move it out of the
    # "negotiation_ongoing" state — both parties agreed, so it's approved (payable).
    booking.amount_cents = neg.current_offer_cents
    booking.status = "approved"

    offer = NegotiationOffer(
        negotiation_id=negotiation_id,
        proposed_by=caller_user_id,
        action="accept",
        amount_cents=neg.current_offer_cents,
        message=None,
        created_at=now,
    )
    db.add(offer)
    db.commit()

    offers = (
        db.query(NegotiationOffer)
        .filter(NegotiationOffer.negotiation_id == negotiation_id)
        .order_by(NegotiationOffer.created_at.asc())
        .all()
    )

    caller = db.query(User).filter(User.user_id == caller_user_id).first()
    caller_name = f"{caller.f_name} {caller.l_name}" if caller else "Someone"
    _notify(
        other_party_id,
        title="Offer Accepted!",
        body=f"{caller_name} accepted ${neg.current_offer_cents / 100:.2f}. The price is agreed.",
        data={"booking_id": neg.booking_id, "negotiation_id": negotiation_id, "type": "negotiation_accepted"},
        db=db,
    )

    return _negotiation_dict(neg, offers, db)


def reject_offer(*, negotiation_id: str, message: str | None, caller_user_id: str, db: Session) -> dict:
    """Reject and close the negotiation. Booking price stays at original."""
    neg = db.query(Negotiation).filter(Negotiation.negotiation_id == negotiation_id).first()
    if not neg:
        raise NegotiationError(404, "Negotiation not found")
    if neg.status != "open":
        raise NegotiationError(400, f"Negotiation is already '{neg.status}'")

    booking = db.query(Booking).filter(Booking.booking_id == neg.booking_id).first()
    other_party_id = _assert_is_party(booking, caller_user_id, db)

    now = datetime.now(timezone.utc)
    neg.status = "rejected"
    neg.updated_at = now

    # Negotiation closed without agreement — return the booking to "pending" so it
    # isn't stuck in "negotiation_ongoing" (the listed price stands).
    if booking.status == "negotiation_ongoing":
        booking.status = "pending"

    offer = NegotiationOffer(
        negotiation_id=negotiation_id,
        proposed_by=caller_user_id,
        action="reject",
        amount_cents=None,
        message=message,
        created_at=now,
    )
    db.add(offer)
    db.commit()

    offers = (
        db.query(NegotiationOffer)
        .filter(NegotiationOffer.negotiation_id == negotiation_id)
        .order_by(NegotiationOffer.created_at.asc())
        .all()
    )

    caller = db.query(User).filter(User.user_id == caller_user_id).first()
    caller_name = f"{caller.f_name} {caller.l_name}" if caller else "Someone"
    _notify(
        other_party_id,
        title="Offer Rejected",
        body=f"{caller_name} declined the offer.",
        data={"booking_id": neg.booking_id, "negotiation_id": negotiation_id, "type": "negotiation_rejected"},
        db=db,
    )

    return _negotiation_dict(neg, offers, db)
