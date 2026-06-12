"""Business logic for event bundles — grouping multiple bookings under one event."""

import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.db.models import Booking, Bundle, Event, Service, User, Vendor

logger = logging.getLogger(__name__)


class BundleError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ── Helpers ───────────────────────────────────────────────────────────


def _booking_summary(booking: Booking, db: Session) -> dict:
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    vendor_user = db.query(User).filter(User.user_id == vendor.user_id).first() if vendor else None
    price = (booking.amount_cents / 100) if booking.amount_cents else (service.price if service else 0.0)
    return {
        "booking_id": booking.booking_id,
        "status": booking.status,
        "payment_status": booking.payment_status,
        "date_iso": booking.date_iso,
        "time_start": booking.time_start,
        "time_end": booking.time_end,
        "location": booking.location,
        "service_name": service.name if service else None,
        "service_category": service.category if service else None,
        "vendor_name": f"{vendor_user.f_name} {vendor_user.l_name}" if vendor_user else None,
        "vendor_id": booking.vendor_id,
        "price": price,
        "amount_cents": booking.amount_cents,
        "open_to_price_negotiation": vendor.open_to_price_negotiation if vendor else False,
    }


def _bundle_dict(bundle: Bundle, bookings: list[Booking], db: Session) -> dict:
    booking_summaries = [_booking_summary(b, db) for b in bookings]
    total_cost = sum(b["price"] for b in booking_summaries)

    status_counts: dict[str, int] = {}
    for b in booking_summaries:
        status_counts[b["status"]] = status_counts.get(b["status"], 0) + 1

    event = db.query(Event).filter(Event.event_id == bundle.event_id).first() if bundle.event_id else None

    return {
        "bundle_id": bundle.bundle_id,
        "user_id": bundle.user_id,
        "name": bundle.name,
        "event_name": bundle.event_name,
        "status": bundle.status,
        "event_id": bundle.event_id,
        "event": {
            "event_id": event.event_id,
            "name": event.name,
            "date_iso": event.date_iso,
            "location": event.location,
            "event_type": event.event_type,
            "guest_count": event.guest_count,
            "budget": event.budget,
        } if event else None,
        "bookings": booking_summaries,
        "booking_count": len(booking_summaries),
        "total_estimated_cost": round(total_cost, 2),
        "status_breakdown": status_counts,
        "created_at": bundle.created_at.isoformat(),
        "updated_at": bundle.updated_at.isoformat(),
    }


def _assert_owns_booking(booking: Booking, caller_user_id: str) -> None:
    if booking.user_id != caller_user_id:
        raise BundleError(403, "You can only add your own bookings to a bundle")


def _assert_owns_bundle(bundle: Bundle, caller_user_id: str) -> None:
    if bundle.user_id != caller_user_id:
        raise BundleError(403, "You do not own this bundle")


# ── Service functions ─────────────────────────────────────────────────


def create_bundle(
    *,
    user_id: str,
    name: str,
    event_name: str | None = None,
    event_id: str | None = None,
    booking_ids: list[str],
    db: Session,
) -> dict:
    """Create a bundle, optionally linking an event and existing bookings."""
    if event_id:
        event = db.query(Event).filter(Event.event_id == event_id).first()
        if not event:
            raise BundleError(404, "Event not found")
        if event.user_id != user_id:
            raise BundleError(403, "You do not own this event")

    now = datetime.now(timezone.utc)
    bundle = Bundle(
        user_id=user_id,
        event_id=event_id,
        name=name,
        event_name=event_name,
        status="draft",
        created_at=now,
        updated_at=now,
    )
    db.add(bundle)
    db.flush()

    bookings = []
    for bid in booking_ids:
        booking = db.query(Booking).filter(Booking.booking_id == bid).first()
        if not booking:
            raise BundleError(404, f"Booking {bid} not found")
        _assert_owns_booking(booking, user_id)
        if booking.bundle_id:
            raise BundleError(400, f"Booking {bid} already belongs to another bundle")
        booking.bundle_id = bundle.bundle_id
        bookings.append(booking)

    db.commit()
    return _bundle_dict(bundle, bookings, db)


def get_bundle(*, bundle_id: str, caller_user_id: str, db: Session) -> dict:
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise BundleError(404, "Bundle not found")
    _assert_owns_bundle(bundle, caller_user_id)
    bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    return _bundle_dict(bundle, bookings, db)


def list_bundles(*, user_id: str, db: Session) -> list[dict]:
    bundles = db.query(Bundle).filter(Bundle.user_id == user_id).order_by(Bundle.created_at.desc()).all()
    result = []
    for b in bundles:
        bookings = db.query(Booking).filter(Booking.bundle_id == b.bundle_id).all()
        result.append(_bundle_dict(b, bookings, db))
    return result


def get_bundle_conversations(*, bundle_id: str, caller_user_id: str, db: Session) -> list[dict]:
    """Return group conversations for a bundle.
    Accessible by the bundle owner (client) or any vendor who is a member of the bundle's conversations.
    """
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise BundleError(404, "Bundle not found")

    # Allow the bundle owner or any conversation member
    from app.db.models import Conversation, ConversationMember
    is_owner = bundle.user_id == caller_user_id
    if not is_owner:
        is_member = db.query(ConversationMember).join(
            Conversation, Conversation.conversation_id == ConversationMember.conversation_id
        ).filter(
            Conversation.bundle_id == bundle_id,
            ConversationMember.user_id == caller_user_id,
        ).first()
        if not is_member:
            raise BundleError(403, "You are not a party to this bundle")

    from app.db.models import Conversation, ConversationMember, GroupMessage
    from app.services.conversation_service import _conversation_dict

    conversations = (
        db.query(Conversation)
        .filter(Conversation.bundle_id == bundle_id)
        .order_by(Conversation.type)
        .all()
    )
    result = []
    from app.db.models import User
    for conv in conversations:
        member_rows = db.query(ConversationMember).filter(
            ConversationMember.conversation_id == conv.conversation_id
        ).all()
        user_ids = [m.user_id for m in member_rows]
        members = db.query(User).filter(User.user_id.in_(user_ids)).all()
        last_msg = (
            db.query(GroupMessage)
            .filter(GroupMessage.conversation_id == conv.conversation_id)
            .order_by(GroupMessage.created_at.desc())
            .first()
        )
        result.append(_conversation_dict(conv, members, last_msg))
    return result


def add_booking_to_bundle(*, bundle_id: str, booking_id: str, caller_user_id: str, db: Session) -> dict:
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise BundleError(404, "Bundle not found")
    _assert_owns_bundle(bundle, caller_user_id)

    if bundle.status == "cancelled":
        raise BundleError(400, "Cannot add bookings to a cancelled bundle")

    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise BundleError(404, "Booking not found")
    _assert_owns_booking(booking, caller_user_id)

    if booking.bundle_id == bundle_id:
        raise BundleError(400, "Booking is already in this bundle")
    if booking.bundle_id:
        raise BundleError(400, "Booking already belongs to another bundle")

    booking.bundle_id = bundle_id
    bundle.updated_at = datetime.now(timezone.utc)
    db.commit()

    # Add the new vendor to all bundle conversations
    try:
        from app.services.conversation_service import add_vendor_to_bundle_conversations
        vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
        if vendor:
            add_vendor_to_bundle_conversations(bundle_id=bundle_id, vendor_user_id=vendor.user_id, db=db)
    except Exception as exc:
        logger.warning("Failed to add vendor to conversations: %s", exc)

    bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    return _bundle_dict(bundle, bookings, db)


def remove_booking_from_bundle(*, bundle_id: str, booking_id: str, caller_user_id: str, db: Session) -> dict:
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise BundleError(404, "Bundle not found")
    _assert_owns_bundle(bundle, caller_user_id)

    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise BundleError(404, "Booking not found")
    if booking.bundle_id != bundle_id:
        raise BundleError(400, "Booking is not in this bundle")

    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    booking.bundle_id = None
    bundle.updated_at = datetime.now(timezone.utc)
    db.commit()

    # Remove vendor from conversations if they have no other bookings in the bundle
    try:
        from app.services.conversation_service import remove_vendor_from_bundle_conversations
        if vendor:
            remove_vendor_from_bundle_conversations(bundle_id=bundle_id, vendor_user_id=vendor.user_id, db=db)
    except Exception as exc:
        logger.warning("Failed to remove vendor from conversations: %s", exc)

    bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    return _bundle_dict(bundle, bookings, db)


def rename_bundle(*, bundle_id: str, name: str, caller_user_id: str, db: Session) -> dict:
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise BundleError(404, "Bundle not found")
    _assert_owns_bundle(bundle, caller_user_id)

    # event_name takes precedence over name when displaying the bundle's title
    # (see _bundle_dict / BundleDetailResponse), so update both to be safe.
    bundle.name = name
    bundle.event_name = name
    bundle.updated_at = datetime.now(timezone.utc)
    db.commit()

    bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    return _bundle_dict(bundle, bookings, db)


def update_bundle_status(*, bundle_id: str, status: str, caller_user_id: str, db: Session) -> dict:
    valid = {"draft", "confirmed", "completed", "cancelled"}
    if status not in valid:
        raise BundleError(400, f"Status must be one of: {', '.join(sorted(valid))}")

    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise BundleError(404, "Bundle not found")
    _assert_owns_bundle(bundle, caller_user_id)

    if status == "confirmed" and bundle.status != "draft":
        raise BundleError(400, "Only a draft bundle can be confirmed")

    prev_status = bundle.status
    bundle.status = status
    bundle.updated_at = datetime.now(timezone.utc)
    db.commit()

    # Create group chats when the bundle is confirmed for the first time
    if status == "confirmed" and prev_status == "draft":
        try:
            from app.services.conversation_service import create_bundle_conversations
            create_bundle_conversations(
                bundle_id=bundle_id, client_user_id=caller_user_id, db=db
            )
        except Exception as exc:
            logger.warning("Failed to create bundle conversations on confirm: %s", exc)

    bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    return _bundle_dict(bundle, bookings, db)


def select_bundle(*, bundle_id: str, caller_user_id: str, db: Session) -> dict:
    """Pick one bundle from a comparison group, delete the other two, and notify vendors.

    The chosen bundle has its bundle_group_id cleared so it behaves like a normal
    draft bundle going forward.
    """
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise BundleError(404, "Bundle not found")
    _assert_owns_bundle(bundle, caller_user_id)

    if not bundle.bundle_group_id:
        raise BundleError(400, "This bundle is not part of a comparison group")

    # Delete the unchosen bundles and their bookings
    others = (
        db.query(Bundle)
        .filter(
            Bundle.bundle_group_id == bundle.bundle_group_id,
            Bundle.bundle_id != bundle_id,
            Bundle.user_id == caller_user_id,
        )
        .all()
    )
    for other in others:
        db.query(Booking).filter(Booking.bundle_id == other.bundle_id).delete()
        db.delete(other)

    bundle.bundle_group_id = None
    bundle.updated_at = datetime.now(timezone.utc)
    db.commit()

    # Now notify vendors for the chosen bundle's bookings
    chosen_bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    event_name = bundle.event_name or bundle.name
    try:
        from app.services.booking_service import _get_booking_parties, _dispatch_status_notification
        for booking in chosen_bookings:
            try:
                client, _, vendor_user, service = _get_booking_parties(db, booking)
                _dispatch_status_notification("pending", booking, client, vendor_user, service, event_name=event_name)
            except Exception as exc:
                logger.warning("select_bundle notification failed for %s: %s", booking.booking_id, exc)
    except Exception as exc:
        logger.warning("select_bundle: could not import notification helpers: %s", exc)

    return _bundle_dict(bundle, chosen_bookings, db)


def delete_bundle(*, bundle_id: str, caller_user_id: str, db: Session) -> None:
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise BundleError(404, "Bundle not found")
    _assert_owns_bundle(bundle, caller_user_id)

    try:
        # Delete the bundle's bookings, along with any negotiations, messages,
        # and reviews tied to those bookings
        from app.db.models import Message, Negotiation, NegotiationOffer, Review
        bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
        for booking in bookings:
            negotiation = db.query(Negotiation).filter(Negotiation.booking_id == booking.booking_id).first()
            if negotiation:
                db.query(NegotiationOffer).filter(
                    NegotiationOffer.negotiation_id == negotiation.negotiation_id).delete()
                db.delete(negotiation)
            db.query(Message).filter(Message.booking_id == booking.booking_id).delete()
            db.query(Review).filter(Review.booking_id == booking.booking_id).delete()
            db.delete(booking)

        # Clean up group conversations and their messages/members
        from app.db.models import Conversation, ConversationMember, GroupMessage, GroupMessageRead
        conversations = db.query(Conversation).filter(Conversation.bundle_id == bundle_id).all()
        for conv in conversations:
            msg_ids = [m.message_id for m in db.query(GroupMessage).filter(
                GroupMessage.conversation_id == conv.conversation_id).all()]
            if msg_ids:
                db.query(GroupMessageRead).filter(GroupMessageRead.message_id.in_(msg_ids)).delete()
                db.query(GroupMessage).filter(GroupMessage.conversation_id == conv.conversation_id).delete()
            db.query(ConversationMember).filter(
                ConversationMember.conversation_id == conv.conversation_id).delete()
            db.delete(conv)

        db.delete(bundle)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("delete_bundle failed for bundle %s", bundle_id)
        raise BundleError(500, f"Delete failed: {exc}")
