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


def _booking_summary(
    booking: Booking,
    service: Service | None,
    vendor: Vendor | None,
    vendor_user: User | None,
) -> dict:
    """Build a booking's summary dict from already-resolved related rows.

    Pure (no DB access) so callers can batch-load the Service/Vendor/User once
    via ``_resolve_booking_refs`` instead of issuing three queries per booking.
    """
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
        "service_subcategory": service.subcategory if service else None,
        "vendor_name": f"{vendor_user.f_name} {vendor_user.l_name}" if vendor_user else None,
        "vendor_id": booking.vendor_id,
        "price": price,
        "amount_cents": booking.amount_cents,
        # Negotiation is now per-service (the vendor toggles it per service),
        # not vendor-wide. Key name kept for client compatibility.
        "open_to_price_negotiation": service.negotiable if service else False,
        # Vendor-approval timestamp — the client uses it to show the 24-hour
        # refund window on paid bookings.
        "confirmed_at": booking.confirmed_at.isoformat() if booking.confirmed_at else None,
        # GPS venue check-in timestamps (stored as ISO strings). Lets the client's
        # bundle view show whether the vendor has arrived and checked in.
        "vendor_checked_in_at": booking.vendor_checked_in_at,
        "client_checked_in_at": booking.client_checked_in_at,
    }


# Sentinel so _bundle_dict can tell "no event passed, go fetch it" apart from
# "event passed and it's None" (a bundle with no linked event).
_UNSET = object()


def _resolve_booking_refs(
    bookings: list[Booking], db: Session
) -> tuple[dict[str, Service], dict[str, Vendor], dict[str, User]]:
    """Batch-load the Service/Vendor/User rows referenced by a set of bookings.

    Three queries total (services, vendors, then the vendors' users) regardless
    of how many bookings there are — the fix for the per-booking N+1 in
    ``_booking_summary``. Returns id→row maps.
    """
    service_ids = {b.service_id for b in bookings if b.service_id}
    vendor_ids = {b.vendor_id for b in bookings if b.vendor_id}

    services = (
        {s.service_id: s for s in db.query(Service).filter(Service.service_id.in_(service_ids)).all()}
        if service_ids else {}
    )
    vendors = (
        {v.vendor_id: v for v in db.query(Vendor).filter(Vendor.vendor_id.in_(vendor_ids)).all()}
        if vendor_ids else {}
    )
    user_ids = {v.user_id for v in vendors.values() if v.user_id}
    users = (
        {u.user_id: u for u in db.query(User).filter(User.user_id.in_(user_ids)).all()}
        if user_ids else {}
    )
    return services, vendors, users


def _bundle_dict(
    bundle: Bundle,
    bookings: list[Booking],
    db: Session,
    *,
    refs: tuple[dict[str, Service], dict[str, Vendor], dict[str, User]] | None = None,
    event=_UNSET,
) -> dict:
    """Serialize a bundle with its bookings.

    ``refs`` (service/vendor/user maps) and ``event`` can be supplied
    pre-resolved by a batch caller (``list_bundles``) to avoid per-bundle
    queries; when omitted they're resolved here so single-bundle callers stay
    a one-liner.
    """
    if refs is None:
        refs = _resolve_booking_refs(bookings, db)
    service_map, vendor_map, user_map = refs

    booking_summaries = []
    for b in bookings:
        vendor = vendor_map.get(b.vendor_id)
        vendor_user = user_map.get(vendor.user_id) if vendor else None
        booking_summaries.append(
            _booking_summary(b, service_map.get(b.service_id), vendor, vendor_user)
        )

    total_cost = sum(b["price"] for b in booking_summaries)

    status_counts: dict[str, int] = {}
    for b in booking_summaries:
        status_counts[b["status"]] = status_counts.get(b["status"], 0) + 1

    if event is _UNSET:
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


def _ensure_bundle_event(bundle: Bundle, db: Session) -> None:
    """Back a bundle with a real Event so its bookings show up under the client's
    My Events / Event Portfolio. Creates one from the bundle's name + a sample
    booking's date/location when the bundle isn't linked yet. No-op once linked
    or when the bundle has no bookings to anchor the event on.

    Does NOT commit — the caller owns the transaction.
    """
    if bundle.event_id:
        return
    sample = (
        db.query(Booking)
        .filter(Booking.bundle_id == bundle.bundle_id)
        .first()
    )
    if not sample:
        return
    # Prefer the booked venue's address for the event location; fall back to a
    # sample booking's location when the bundle has no venue service.
    venue_service = (
        db.query(Service)
        .join(Booking, Booking.service_id == Service.service_id)
        .filter(
            Booking.bundle_id == bundle.bundle_id,
            Service.category == "venue",
            Service.location.isnot(None),
        )
        .first()
    )
    event = Event(
        user_id=bundle.user_id,
        name=(bundle.event_name or bundle.name or "My Event"),
        date_iso=sample.date_iso or "",
        location=(venue_service.location if venue_service else None) or sample.location or "",
    )
    db.add(event)
    db.flush()
    bundle.event_id = event.event_id
    bundle.updated_at = datetime.now(timezone.utc)


def _assert_owns_booking(booking: Booking, caller_user_id: str) -> None:
    if booking.user_id != caller_user_id:
        raise BundleError(403, "You can only add your own bookings to a bundle")


def _assert_owns_bundle(bundle: Bundle, caller_user_id: str) -> None:
    if bundle.user_id != caller_user_id:
        raise BundleError(403, "You do not own this bundle")


def _delete_booking_cascade(booking: Booking, db: Session) -> None:
    """Delete a booking and everything tied to it (negotiation + offers, direct
    messages, reviews). Used when a client removes a booking or deletes a bundle,
    so the booking also disappears from the vendor's side."""
    from app.db.models import Message, Negotiation, NegotiationOffer, Review
    # A booking can have more than one negotiation (re-negotiation after a reject),
    # so delete them all — not just the first — or the FK constraint on the booking
    # delete is violated.
    negotiation_ids = [
        n.negotiation_id
        for n in db.query(Negotiation).filter(Negotiation.booking_id == booking.booking_id).all()
    ]
    if negotiation_ids:
        db.query(NegotiationOffer).filter(
            NegotiationOffer.negotiation_id.in_(negotiation_ids)
        ).delete(synchronize_session=False)
        db.query(Negotiation).filter(
            Negotiation.negotiation_id.in_(negotiation_ids)
        ).delete(synchronize_session=False)
    db.query(Message).filter(Message.booking_id == booking.booking_id).delete(synchronize_session=False)
    db.query(Review).filter(Review.booking_id == booking.booking_id).delete(synchronize_session=False)
    # Flush so the dependent rows are gone in the DB before the booking row is
    # removed — without mapped relationships, SQLAlchemy won't order this for us.
    db.flush()
    db.delete(booking)


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


def list_bundles(
    *, user_id: str, db: Session, limit: int | None = None, offset: int = 0
) -> list[dict]:
    """Return the user's bundles, newest first.

    Batched to a constant number of queries regardless of how many bundles or
    bookings there are (previously 1 + N×2 + N×M×3): one query for the page of
    bundles, one for all their bookings, three to resolve the bookings'
    services/vendors/users, and one for the linked events. ``limit``/``offset``
    page the result.
    """
    query = (
        db.query(Bundle)
        .filter(Bundle.user_id == user_id)
        .order_by(Bundle.created_at.desc())
    )
    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    bundles = query.all()
    if not bundles:
        return []

    # All bookings for the returned bundles in one query, grouped by bundle.
    bundle_ids = [b.bundle_id for b in bundles]
    all_bookings = db.query(Booking).filter(Booking.bundle_id.in_(bundle_ids)).all()
    bookings_by_bundle: dict[str, list[Booking]] = {}
    for bk in all_bookings:
        bookings_by_bundle.setdefault(bk.bundle_id, []).append(bk)

    # Resolve every booking's Service/Vendor/User once, and every linked event.
    refs = _resolve_booking_refs(all_bookings, db)
    event_ids = {b.event_id for b in bundles if b.event_id}
    event_map = (
        {e.event_id: e for e in db.query(Event).filter(Event.event_id.in_(event_ids)).all()}
        if event_ids else {}
    )

    return [
        _bundle_dict(
            b,
            bookings_by_bundle.get(b.bundle_id, []),
            db,
            refs=refs,
            event=event_map.get(b.event_id),
        )
        for b in bundles
    ]


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

    # Don't delete a booking that's already been paid for — money is involved.
    if booking.payment_status in ("paid", "released", "disputed"):
        raise BundleError(400, "Can't remove a booking that's already been paid")

    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    # Delete the booking outright so it also disappears from the vendor's side.
    _delete_booking_cascade(booking, db)
    bundle.updated_at = datetime.now(timezone.utc)
    db.flush()

    # Remove vendor from conversations if they have no other bookings in the bundle
    try:
        from app.services.conversation_service import remove_vendor_from_bundle_conversations
        if vendor:
            remove_vendor_from_bundle_conversations(bundle_id=bundle_id, vendor_user_id=vendor.user_id, db=db)
    except Exception as exc:
        logger.warning("Failed to remove vendor from conversations: %s", exc)

    db.commit()

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
            db.rollback()
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

    # Back the chosen bundle with a real Event so its services show up under the
    # client's My Events / Event Portfolio.
    _ensure_bundle_event(bundle, db)
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


def _delete_bundle_cascade(bundle: Bundle, db: Session) -> None:
    """Delete a bundle plus everything hanging off it: its bookings (with their
    negotiations/messages/reviews), its group conversations, and — once no other
    bundle references it — its linked event. Flushes but does NOT commit; the
    caller owns the transaction."""
    bundle_id = bundle.bundle_id
    # Remember the linked event so we can clean it up after the bundle is gone.
    event_id = bundle.event_id

    # Delete the bundle's bookings, along with any negotiations, messages,
    # and reviews tied to those bookings (also removes them from vendors).
    bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    for booking in bookings:
        _delete_booking_cascade(booking, db)

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

    # Flush so the booking/conversation deletes are applied before the
    # bundle delete — without a relationship() linking these mappers,
    # SQLAlchemy's flush ordering doesn't guarantee that on its own,
    # and deleting the bundle first trips the FK constraints.
    db.flush()

    db.delete(bundle)
    db.flush()

    # Cascade-delete the bundle's event once no other bundle references it.
    # This removes the auto-created events that back AI bundles so they don't
    # linger in the client's Event Portfolio after the bundle is gone.
    if event_id:
        still_referenced = (
            db.query(Bundle)
            .filter(Bundle.event_id == event_id, Bundle.bundle_id != bundle_id)
            .count()
        )
        if still_referenced == 0:
            event = db.query(Event).filter(Event.event_id == event_id).first()
            if event:
                db.delete(event)


def delete_bundle(*, bundle_id: str, caller_user_id: str, db: Session) -> None:
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise BundleError(404, "Bundle not found")
    _assert_owns_bundle(bundle, caller_user_id)

    try:
        _delete_bundle_cascade(bundle, db)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("delete_bundle failed for bundle %s", bundle_id)
        raise BundleError(500, f"Delete failed: {exc}")


# ── One-off legacy data cleanup ───────────────────────────────────────

# Marker the pre-fix iOS app wrote on events it auto-created for AI bundles.
_LEGACY_AI_EVENT_MARKER = "Bundle from jornAI"

# A booking with any of these payment states is never deleted by the cleanup.
_PROTECTED_PAYMENT_STATUSES = {"processing", "paid", "released", "refunded", "disputed"}


def cleanup_legacy_bundle_event_data(*, db: Session, dry_run: bool = True, stale_days: int = 7) -> dict:
    """One-off cleanup for data created before bundles were backed by events
    (2026-07 linkage fix). Three targets:

    1. Orphan AI events — events the old iOS confirm flow created (marked by
       description "Bundle from jornAI") that no bundle references. User-created
       events are never touched.
    2. Abandoned comparison drafts — draft bundles still carrying a
       bundle_group_id (unchosen chatbot options) older than `stale_days`.
       Skipped entirely if any of their bookings has payment activity.
    3. Duplicate bookings — more than one booking for the same
       (bundle, vendor, service, date), from the era when select+confirm both
       created bookings. Keeps the most meaningful one (payment activity >
       has a negotiation > has a real location > furthest along) and
       cascade-deletes the rest. Paid/processing bookings are never deleted.

    With dry_run=True (the default) nothing is modified — the report shows
    exactly what a real run would delete.
    """
    from collections import defaultdict
    from datetime import datetime, timedelta
    from app.db.models import Negotiation

    report: dict = {
        "dry_run": dry_run,
        "orphan_ai_events": [],
        "stale_draft_bundles": [],
        "duplicate_bookings_removed": [],
        "orphan_conversations": [],
        "skipped_paid_groups": 0,
    }

    # ── 1. Orphan AI-generated events ─────────────────────────────────
    referenced_event_ids = {
        eid for (eid,) in db.query(Bundle.event_id).filter(Bundle.event_id.isnot(None)).all()
    }
    ai_events = db.query(Event).filter(Event.description == _LEGACY_AI_EVENT_MARKER).all()
    for event in ai_events:
        if event.event_id in referenced_event_ids:
            continue
        report["orphan_ai_events"].append({"event_id": event.event_id, "name": event.name})
        if not dry_run:
            db.delete(event)

    # ── 2. Abandoned comparison drafts ────────────────────────────────
    # Column is a naive-UTC timestamp; compare with a naive cutoff.
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=stale_days)
    stale_bundles = (
        db.query(Bundle)
        .filter(
            Bundle.status == "draft",
            Bundle.bundle_group_id.isnot(None),
            Bundle.created_at < cutoff,
        )
        .all()
    )
    for bundle in stale_bundles:
        bookings = db.query(Booking).filter(Booking.bundle_id == bundle.bundle_id).all()
        if any(b.payment_status in _PROTECTED_PAYMENT_STATUSES for b in bookings):
            report["skipped_paid_groups"] += 1
            continue
        report["stale_draft_bundles"].append({
            "bundle_id": bundle.bundle_id,
            "name": bundle.name,
            "bookings": len(bookings),
        })
        if not dry_run:
            _delete_bundle_cascade(bundle, db)

    # ── 3. Duplicate bookings within a bundle ─────────────────────────
    if not dry_run:
        db.flush()
    # Bundles slated for deletion in stage 2 — skip their bookings so a dry
    # run doesn't double-report rows that vanish with their bundle anyway.
    stale_bundle_ids = {entry["bundle_id"] for entry in report["stale_draft_bundles"]}
    groups: dict = defaultdict(list)
    for booking in db.query(Booking).filter(Booking.bundle_id.isnot(None)).all():
        if booking.bundle_id in stale_bundle_ids:
            continue
        groups[(booking.bundle_id, booking.vendor_id, booking.service_id, booking.date_iso)].append(booking)

    negotiated_ids = {
        bid for (bid,) in db.query(Negotiation.booking_id).all()
    }

    def keeper_rank(b: Booking) -> tuple:
        has_payment = b.payment_status in _PROTECTED_PAYMENT_STATUSES
        has_negotiation = b.booking_id in negotiated_ids
        has_location = bool(b.location) and b.location.upper() != "TBD"
        progressed = b.status not in ("pending",)
        return (has_payment, has_negotiation, has_location, progressed)

    for _, members in groups.items():
        if len(members) < 2:
            continue
        # Never delete anything in a group with more than one payment-active
        # booking — that needs human eyes, not a script.
        paid = [b for b in members if b.payment_status in _PROTECTED_PAYMENT_STATUSES]
        if len(paid) > 1:
            report["skipped_paid_groups"] += 1
            continue
        keeper = max(members, key=keeper_rank)
        for booking in members:
            if booking.booking_id == keeper.booking_id:
                continue
            if booking.payment_status in _PROTECTED_PAYMENT_STATUSES:
                continue  # belt and braces — never remove payment-active rows
            report["duplicate_bookings_removed"].append({
                "booking_id": booking.booking_id,
                "bundle_id": booking.bundle_id,
                "kept": keeper.booking_id,
            })
            if not dry_run:
                _delete_booking_cascade(booking, db)

    # ── 4. Orphaned group chats ───────────────────────────────────────
    # Conversations whose bundle no longer exists (deleted before bundle
    # deletion cascaded to chats). Remove them with members/messages/reads.
    from app.db.models import Conversation, ConversationMember, GroupMessage, GroupMessageRead
    if not dry_run:
        db.flush()
    all_convs = db.query(Conversation).all()
    conv_bundle_ids = {c.bundle_id for c in all_convs}
    live_bundles = {
        bid for (bid,) in db.query(Bundle.bundle_id).filter(Bundle.bundle_id.in_(conv_bundle_ids)).all()
    } if conv_bundle_ids else set()
    for conv in all_convs:
        if conv.bundle_id in live_bundles:
            continue
        report["orphan_conversations"].append({
            "conversation_id": conv.conversation_id,
            "name": conv.name,
        })
        if not dry_run:
            msg_ids = [m.message_id for m in db.query(GroupMessage).filter(
                GroupMessage.conversation_id == conv.conversation_id).all()]
            if msg_ids:
                db.query(GroupMessageRead).filter(GroupMessageRead.message_id.in_(msg_ids)).delete(synchronize_session=False)
                db.query(GroupMessage).filter(GroupMessage.conversation_id == conv.conversation_id).delete(synchronize_session=False)
            db.query(ConversationMember).filter(
                ConversationMember.conversation_id == conv.conversation_id).delete(synchronize_session=False)
            db.delete(conv)

    if dry_run:
        db.rollback()
    else:
        db.commit()

    report["totals"] = {
        "orphan_ai_events": len(report["orphan_ai_events"]),
        "stale_draft_bundles": len(report["stale_draft_bundles"]),
        "duplicate_bookings_removed": len(report["duplicate_bookings_removed"]),
        "orphan_conversations": len(report["orphan_conversations"]),
    }
    return report
