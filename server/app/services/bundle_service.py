"""Business logic for event bundles — grouping multiple bookings under one event."""

import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.db.models import Booking, Bundle, Event, Service, User, Vendor
from app.models.schemas import PaymentStatus

logger = logging.getLogger(__name__)


class BundleError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# Payment states meaning money has moved, is moving, or moved and came back.
#
# A booking in any of them is a financial record before it is a plan item: it is
# the only thing tying a Stripe charge to what the charge was for, who owed it
# and who was owed. Deleting it doesn't undo the payment — it strands it.
#
# 'processing' is in here because money in flight is the worst case, not an
# exempt one. 'refunded' is too: the money came back, and the record of that is
# the evidence it did.
#
# This was written down once already, at the bottom of the file, and used only
# by the one-off legacy cleanup — while the live delete paths each had their own
# idea. remove_booking_from_bundle checked three of the five; _delete_bundle_
# cascade checked none at all, so a plan holding escrow could be deleted whole
# when the same booking could not be removed singly.
MONEY_MOVED_STATUSES = frozenset(
    {
        PaymentStatus.PROCESSING.value,
        PaymentStatus.PAID.value,
        PaymentStatus.RELEASED.value,
        PaymentStatus.REFUNDED.value,
        PaymentStatus.DISPUTED.value,
    }
)


def _money_has_moved(booking: Booking) -> bool:
    return (booking.payment_status or PaymentStatus.UNPAID.value) in MONEY_MOVED_STATUSES


def _latest_change_requests(bookings: list[Booking], db: Session):
    """The newest date change per booking, in one query.

    Batched for the same reason the service/vendor/user maps are: a screen
    listing many plans must not pay a query per booking for this, and there is a
    test that fails if it does.
    """
    from app.db.models import ChangeRequest

    ids = [b.booking_id for b in bookings if b.booking_id]
    if not ids:
        return {}
    rows = (
        db.query(ChangeRequest)
        .filter(ChangeRequest.booking_id.in_(ids))
        .order_by(ChangeRequest.created_at.asc())
        .all()
    )
    # Ascending, so the last write per booking is the newest.
    return {cr.booking_id: cr for cr in rows}


def _open_negotiations(bookings: list[Booking], db: Session):
    """The open Negotiation per booking (only those in negotiation_ongoing
    status can have one), in one query — same batching reason as
    _latest_change_requests.
    """
    from app.db.models import Negotiation

    ids = [b.booking_id for b in bookings if b.booking_id and b.status == "negotiation_ongoing"]
    if not ids:
        return {}
    rows = (
        db.query(Negotiation)
        .filter(Negotiation.booking_id.in_(ids), Negotiation.status == "open")
        .all()
    )
    return {neg.booking_id: neg for neg in rows}


def _change_request_summary(cr) -> dict | None:
    """One date change, as the client's plan reads it.

    Pending ones always: somebody owes an answer. Settled ones only while they
    still imply something to do — a decline leaves the client choosing between
    keeping the booking and taking a refund, and an expiry leaves the same
    choice with nobody having answered at all. An accepted one is just the
    booking's dates now, which the row already shows.
    """
    if not cr or cr.status in ("accepted", "withdrawn"):
        return None
    return {
        "change_request_id": cr.change_request_id,
        "status": cr.status,
        "date_iso": cr.date_iso,
        "date_end": cr.date_end,
        "time_start": cr.time_start,
        "time_end": cr.time_end,
        "message": cr.message,
        "response_message": cr.response_message,
        # Its presence is the "needs your approval" state.
        "repriced_amount_cents": cr.repriced_amount_cents,
        "created_at": cr.created_at.isoformat() if cr.created_at else None,
    }


# ── Helpers ───────────────────────────────────────────────────────────


def _refund_preview(booking: Booking) -> dict:
    """cancellation_split's numbers, shaped for a client that just wants the
    countdown — pure, so it's cheap to compute for every booking in a list."""
    from datetime import datetime, timedelta, timezone
    from app.services.stripe_service import GRACE_HOURS, cancellation_split

    now = datetime.now(timezone.utc)
    split = cancellation_split(booking, now)
    confirmed_at = booking.confirmed_at
    if confirmed_at is not None and confirmed_at.tzinfo is None:
        confirmed_at = confirmed_at.replace(tzinfo=timezone.utc)
    amount = booking.amount_cents or 0
    return {
        "full_refund_until": (
            (confirmed_at + timedelta(hours=GRACE_HOURS)).isoformat() if confirmed_at else None
        ),
        "vendor_pct_now": (
            round(split["vendor_cents"] / amount * 100, 1) if amount else 0.0
        ),
        "client_refund_now_cents": split["refund_to_client_cents"],
    }


def _booking_summary(
    booking: Booking,
    service: Service | None,
    vendor: Vendor | None,
    vendor_user: User | None,
    change_request=None,
    negotiation=None,
) -> dict:
    """Build a booking's summary dict from already-resolved related rows.

    Pure (no DB access) so callers can batch-load the Service/Vendor/User once
    via ``_resolve_booking_refs`` instead of issuing three queries per booking.
    """
    from app.services.booking_service import (
        _DEAD_BOOKING_STATUSES,
        _DEAD_VENUE_PAYMENT_STATUSES,
        _zone_name,
        negotiation_role_from,
        resolve_total_cents,
    )
    # Resolved total: stored amount, else recomputed rate x quantity, else the
    # flat price for event-priced services. None => rate-priced with an unknown
    # quantity, so show the rate + unit, not a total masquerading as one.
    total_cents = resolve_total_cents(booking, service)
    price = (total_cents / 100) if total_cents is not None else (service.price if service else 0.0)
    return {
        "booking_id": booking.booking_id,
        "status": booking.status,
        "payment_status": booking.payment_status,
        # The single source of truth for "can anything further happen to
        # this booking" — see the identical field/comment on
        # booking_service._booking_dict. jorna-website's planning.ts used to
        # re-derive this from a hand-mirrored copy of the same two constants.
        "is_dead": (
            booking.status in _DEAD_BOOKING_STATUSES
            or (booking.payment_status or PaymentStatus.UNPAID.value) in _DEAD_VENUE_PAYMENT_STATUSES
        ),
        "date_iso": booking.date_iso,
        # The quantity a rate-priced service multiplies by. Exposed so a client
        # swapping one service for another can carry the quantity across —
        # dropping it would leave the replacement unpayable (price_pending_quantity).
        "date_end": booking.date_end,
        "guest_count": booking.guest_count,
        "performer_count": booking.performer_count,
        # Denormalized from the service so a client can run its own gap-check
        # (bookingGaps()) against the booking alone — see plan_readiness.booking_gaps.
        "require_guest_count": bool(service.require_guest_count) if service else False,
        "require_performer_count": bool(service.require_performer_count) if service else False,
        # What the client said when requesting this — see BookingCreate.client_note.
        "client_note": booking.client_note,
        "time_start": booking.time_start,
        "time_end": booking.time_end,
        "location": booking.location,
        "service_name": service.name if service else None,
        "service_category": service.category if service else None,
        "service_subcategory": service.subcategory if service else None,
        "vendor_name": f"{vendor_user.f_name} {vendor_user.l_name}" if vendor_user else None,
        "vendor_id": booking.vendor_id,
        # "stripe" (protected, escrow-held) or "manual" (paid directly via
        # Venmo/Zelle) — a snapshot of the vendor's setting at send time, not
        # read live from the vendor. Null predates this feature; treated as
        # "stripe" everywhere it's read.
        "payment_method": booking.payment_method,
        # Only meaningful (and only sent) on a manual-track booking — where to
        # actually send the money, since Jorna isn't collecting it.
        "vendor_venmo_handle": vendor.venmo_handle if vendor and booking.payment_method == "manual" else None,
        "vendor_zelle_contact": vendor.zelle_contact if vendor and booking.payment_method == "manual" else None,
        "price": price,
        "price_unit": service.price_unit if service else None,
        "price_pending_quantity": total_cents is None,
        "amount_cents": booking.amount_cents,
        # Negotiation is now per-service (the vendor toggles it per service),
        # not vendor-wide. Key name kept for client compatibility.
        "open_to_price_negotiation": service.negotiable if service else False,
        # Vendor-approval timestamp (when the vendor accepted the request).
        # Also what the 24-hour cancellation grace window runs from — see
        # stripe_service.cancellation_split.
        "confirmed_at": booking.confirmed_at.isoformat() if booking.confirmed_at else None,
        # Escrow lifecycle. Clients need these to show the release state
        # honestly: who still has to confirm, and (via refund_preview below)
        # what cancelling would pay out right now.
        "paid_at": booking.paid_at.isoformat() if booking.paid_at else None,
        # What stripe_service.cancel_booking would pay out this instant — the
        # same numbers the UI's eligibility countdown reads, computed once
        # here rather than reimplemented in JS. Only meaningful while there's
        # something to cancel.
        "refund_preview": (
            _refund_preview(booking) if booking.payment_status == PaymentStatus.PAID.value else None
        ),
        "customer_confirmed_at": (
            booking.customer_confirmed_at.isoformat() if booking.customer_confirmed_at else None
        ),
        "vendor_confirmed_at": (
            booking.vendor_confirmed_at.isoformat() if booking.vendor_confirmed_at else None
        ),
        "funds_released_at": (
            booking.funds_released_at.isoformat() if booking.funds_released_at else None
        ),
        # GPS venue check-in timestamps (stored as ISO strings). Lets the client's
        # bundle view show whether the vendor has arrived and checked in.
        "vendor_checked_in_at": booking.vendor_checked_in_at,
        "client_checked_in_at": booking.client_checked_in_at,
        # The venue's own clock. The escrow gate is "has the event happened
        # yet", which is a question about the calendar at the venue — a client
        # answering it in the browser's timezone reached a different day from
        # the one the server enforces. See booking_service.venue_today.
        "timezone": _zone_name(booking),
        # A date change out with this vendor, or lately settled by them. The
        # client's plan reads it to draw the per-vendor board; a booking with
        # none carries null and nothing renders.
        "change_request": _change_request_summary(change_request),
        # Which side owes the next move on an open price negotiation — see
        # negotiation_role_from. None when there's no live negotiation.
        "negotiation_awaiting_role": negotiation_role_from(booking, negotiation),
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
    change_requests: dict | None = None,
    negotiations: dict | None = None,
    event=_UNSET,
) -> dict:
    """Serialize a bundle with its bookings.

    ``refs`` (service/vendor/user maps), ``change_requests``, ``negotiations``
    and ``event`` can be supplied pre-resolved by a batch caller
    (``list_bundles``) to avoid per-bundle queries; when omitted they're
    resolved here so single-bundle callers stay a one-liner.
    """
    if refs is None:
        refs = _resolve_booking_refs(bookings, db)
    service_map, vendor_map, user_map = refs

    if event is _UNSET:
        event = db.query(Event).filter(Event.event_id == bundle.event_id).first() if bundle.event_id else None

    # Whether this plan has anywhere to check in against. One answer for the
    # whole bundle: checkin_anchor derives it from the bundle's live venue, or
    # failing that the event's address, and both are properties of the bundle
    # rather than of any one booking. Fed the rows already loaded above so a
    # page listing many plans doesn't pay two queries each for something it is
    # holding — the rule itself still lives in checkin_anchor.
    from app.services.booking_service import checkin_anchor
    from app.services.plan_readiness import locked_fields_for
    from app.services.reminder_service import last_reminder_at, resend_state_from, starts_at

    anchor_lat, anchor_lng = (
        checkin_anchor(
            bookings[0], db,
            bundle_bookings=bookings, services=service_map, event=event,
        )
        if bookings
        else (None, None)
    )
    has_anchor = anchor_lat is not None and anchor_lng is not None
    now = datetime.now(timezone.utc)
    # One query for the whole bundle — or none at all, when a batch caller has
    # already asked once for every bundle on the page.
    if change_requests is None:
        change_requests = _latest_change_requests(bookings, db)
    if negotiations is None:
        negotiations = _open_negotiations(bookings, db)

    booking_summaries = []
    for b in bookings:
        vendor = vendor_map.get(b.vendor_id)
        vendor_user = user_map.get(vendor.user_id) if vendor else None
        summary = _booking_summary(
            b, service_map.get(b.service_id), vendor, vendor_user,
            change_request=change_requests.get(b.booking_id),
            negotiation=negotiations.get(b.booking_id),
        )
        # Whether the client may nudge this vendor, from the same rule the
        # endpoint enforces — see resend_state_from.
        resend = resend_state_from(
            b, start=starts_at(b, db), has_anchor=has_anchor, now=now
        )
        last_reminded = last_reminder_at(b)
        summary["can_resend_checkin"] = resend["can_resend"]
        summary["resend_checkin_reason"] = resend["reason"]
        # Which of this booking's details the vendor has already been told, and
        # so may no longer be changed. Empty on a draft. From the same function
        # update_booking enforces, so a field the client can still edit is one
        # the server will still accept.
        summary["locked_fields"] = locked_fields_for(b, reached=bundle.status != "draft")
        summary["checkin_reminded_at"] = (
            last_reminded.isoformat() if last_reminded else None
        )
        booking_summaries.append(summary)

    total_cost = sum(b["price"] for b in booking_summaries)

    status_counts: dict[str, int] = {}
    for b in booking_summaries:
        status_counts[b["status"]] = status_counts.get(b["status"], 0) + 1

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
        # Carried over with the rest. The builder writes the headcount onto
        # every booking it creates and this took the date and the place but not
        # the number, so an event arrived without something its client had
        # already said — and the editor asked for it a second time.
        guest_count=sample.guest_count,
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
    messages, reviews, and any conversation attached to the booking itself rather
    than its bundle). Used when a client removes a booking or deletes a bundle,
    so the booking also disappears from the vendor's side.

    Refuses outright if money has moved. The guard lives here, at the one point
    every delete path passes through, rather than at each caller — because the
    bug this fixes was exactly a caller that didn't have it. A rule stated once
    at the chokepoint cannot be skipped by the next path someone adds.

    Callers that can give a better answer should check first and say something
    useful; this is the backstop, and it raises rather than silently skipping so
    a caller can never believe it deleted something it didn't.
    """
    if _money_has_moved(booking):
        raise BundleError(
            400,
            "This booking has money against it, so it can't be deleted. "
            "Request a refund or raise a problem with the vendor instead — "
            "the record has to outlive the plan.",
        )

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
    # Date changes point at the booking too. Missing here, any plan that had ever
    # had one proposed against it failed to delete on the foreign key — the same
    # way deleting an account failed on a refresh token.
    from app.db.models import ChangeRequest
    db.query(ChangeRequest).filter(
        ChangeRequest.booking_id == booking.booking_id
    ).delete(synchronize_session=False)
    # A conversation can point at a booking directly (subject_type == "booking",
    # e.g. a vendor enquiry that became a booking) rather than at the bundle —
    # _delete_bundle_cascade only ever looks up conversations by bundle_id, so
    # this is the one place that ever cleans up a booking-subject conversation.
    # Missing this is exactly what made vendor account deletion fail: the
    # booking lives on the client's bundle (so the bundle cascade runs), but the
    # conversation was attached to the booking, not the bundle, and outlived it.
    from app.db.models import Conversation, ConversationMember, GroupMessage, GroupMessageRead
    conversations = db.query(Conversation).filter(Conversation.booking_id == booking.booking_id).all()
    for conv in conversations:
        msg_ids = [
            m.message_id
            for m in db.query(GroupMessage).filter(GroupMessage.conversation_id == conv.conversation_id).all()
        ]
        if msg_ids:
            db.query(GroupMessageRead).filter(GroupMessageRead.message_id.in_(msg_ids)).delete(
                synchronize_session=False
            )
            db.query(GroupMessage).filter(GroupMessage.conversation_id == conv.conversation_id).delete(
                synchronize_session=False
            )
        db.query(ConversationMember).filter(
            ConversationMember.conversation_id == conv.conversation_id
        ).delete(synchronize_session=False)
        db.delete(conv)
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

    # One plan, so one chance to give it a pin if it hasn't got one. Plans whose
    # address was written by the builder, by POST /bookings or by iOS never had
    # coordinates — nothing on those paths geocodes — so check-in was impossible
    # on a plan showing a complete address, and the reason given said the plan
    # had no address.
    #
    # Here rather than in list_bundles, which exists to be a constant number of
    # queries and must not become a network call per event. Costs nothing once
    # the pin exists, which after the first load it does.
    if bundle.event_id:
        try:
            from app.services.geocode_service import backfill_event_pin

            event = db.query(Event).filter(Event.event_id == bundle.event_id).first()
            backfill_event_pin(event, db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pin backfill failed for bundle %s: %s", bundle_id, exc)

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
    change_requests = _latest_change_requests(all_bookings, db)
    negotiations = _open_negotiations(all_bookings, db)
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
            change_requests=change_requests,
            negotiations=negotiations,
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
    # Reads the shared set rather than its own list, which was missing
    # 'processing' (a payment in flight, the worst one to lose) and 'refunded'
    # (where the record *is* the evidence the money came back).
    if _money_has_moved(booking):
        raise BundleError(400, "Can't remove a booking that's already been paid")

    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    # Delete the booking outright so it also disappears from the vendor's side.
    _delete_booking_cascade(booking, db)
    bundle.updated_at = datetime.now(timezone.utc)
    db.flush()

    # Re-derive the event's venue anchor: if the removed booking was the venue,
    # this clears the coords so the other vendors don't check in against it.
    from app.services.booking_service import sync_event_venue
    sync_event_venue(bundle_id, db)

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

    # Cancelling a plan that is holding money is the same act as deleting one,
    # minus the tidying up: the vendors keep their bookings, the charge keeps
    # standing, and the plan says it isn't happening. Whatever is owed has to be
    # resolved on the booking that owes it.
    if status == "cancelled":
        held = [
            b for b in db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
            if _money_has_moved(b)
        ]
        if held:
            raise BundleError(
                400,
                f"{len(held)} booking{'s' if len(held) > 1 else ''} on this plan "
                "still ha" + ("ve" if len(held) > 1 else "s") + " money against "
                "it. Refund or resolve "
                f"{'them' if len(held) > 1 else 'it'} before cancelling the plan.",
            )

    prev_status = bundle.status
    bundle.status = status
    bundle.updated_at = datetime.now(timezone.utc)
    db.commit()

    if status == "confirmed" and prev_status == "draft":
        _open_bundle_conversations(bundle_id, caller_user_id, db)

    bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    return _bundle_dict(bundle, bookings, db)


def _open_bundle_conversations(bundle_id: str, caller_user_id: str, db: Session) -> None:
    """Group chats for a bundle that has just become real.

    Shared by the two ways that happens — an explicit status change, and sending
    the bundle to its vendors — so the second doesn't quietly skip them.
    """
    try:
        from app.services.conversation_service import create_bundle_conversations
        create_bundle_conversations(
            bundle_id=bundle_id, client_user_id=caller_user_id, db=db
        )
    except Exception as exc:
        db.rollback()
        logger.warning("Failed to create bundle conversations on confirm: %s", exc)


def select_bundle(*, bundle_id: str, caller_user_id: str, db: Session) -> dict:
    """Send a bundle to its vendors, and — if it came from a comparison group —
    discard the options that weren't chosen.

    Notifying the vendors is what this call is for; the group is incidental.
    Refusing a bundle without one meant "Send to vendors" failed outright for
    every bundle assembled service by service from the marketplace, and again
    for any bundle sent a second time after another service was added, since
    choosing clears the group. Both told the client it wasn't part of a
    comparison group, which is true and is not their problem.

    Only bookings still waiting for an answer are notified. Sending a bundle
    that already has approvals in it should reach the vendors who haven't
    replied, not ask the ones who have to look again.

    Sending also moves the bundle out of "draft", which nothing else did — see
    the comment at the status change.
    """
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise BundleError(404, "Bundle not found")
    _assert_owns_bundle(bundle, caller_user_id)

    # Nothing incomplete reaches a vendor. The web has greyed out its send
    # button on this rule for as long as the button has existed, but only its
    # own — this endpoint accepted anything, so iOS or a direct call sent
    # regardless. A vendor asked to hold a date that isn't set is being asked a
    # question, not given a job.
    from app.services.plan_readiness import describe_gaps, plan_gaps

    # Checked on every send, not only the first. Re-sending a plan asks the
    # vendors who haven't answered yet, and that is a request going out like any
    # other — scoped to those bookings, so a plan already half accepted isn't
    # held up over somebody else's completed one.
    gaps = plan_gaps(
        bundle_id, db, only_statuses=None if bundle.status == "draft" else ("pending",)
    )
    if gaps:
        booking, missing = gaps[0]
        service = db.query(Service).filter(Service.service_id == booking.service_id).first()
        name = service.name if service else "One of these bookings"
        more = f" ({len(gaps) - 1} more still need details.)" if len(gaps) > 1 else ""
        raise BundleError(
            400,
            f"{name} still needs {describe_gaps(missing)}. "
            f"Fill that in before sending — once a request is out, it can't be changed.{more}",
        )

    if bundle.bundle_group_id:
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

        # Cleared so the chosen bundle behaves like any other from here on.
        bundle.bundle_group_id = None

    # Sending is what makes a bundle real, so this is where it stops being a
    # draft. Nothing used to move a bundle off "draft" at all — it was created
    # as one and stayed one for life, which left the clients unable to tell a
    # plan nobody had seen from a plan already half accepted. Both kept
    # offering to send it.
    was_draft = bundle.status == "draft"
    if was_draft:
        bundle.status = "confirmed"

    bundle.updated_at = datetime.now(timezone.utc)

    # Back the chosen bundle with a real Event so its services show up under the
    # client's My Events / Event Portfolio.
    _ensure_bundle_event(bundle, db)
    db.commit()

    # The same thing confirming a bundle does, for the same reason: the people
    # in it now have something to talk about.
    if was_draft:
        _open_bundle_conversations(bundle_id, caller_user_id, db)

    # Now notify vendors for the chosen bundle's bookings
    chosen_bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    event_name = bundle.event_name or bundle.name
    # A vendor who has already answered doesn't need asking again — that's what
    # a second send would otherwise do to every approval in the bundle.
    unanswered = [b for b in chosen_bookings if b.status == "pending"]
    try:
        from app.services.booking_service import _get_booking_parties, _dispatch_status_notification
        for booking in unanswered:
            try:
                client, _, vendor_user, service = _get_booking_parties(db, booking)
                _dispatch_status_notification("pending", booking, client, vendor_user, service, db, event_name=event_name)
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

    # Asked before anything is touched, so the answer can name what's blocking
    # rather than reporting the first booking the cascade happened to reach.
    # The cascade refuses these too — this is the version worth reading.
    held = [
        b for b in db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
        if _money_has_moved(b)
    ]
    if held:
        from app.services.booking_service import resolve_total_cents

        total = 0
        names: list[str] = []
        for b in held:
            service = db.query(Service).filter(Service.service_id == b.service_id).first()
            total += resolve_total_cents(b, service) or 0
            if service and service.name:
                names.append(service.name)
        amount = f"${total / 100:,.0f}" if total else "Money"
        who = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
        raise BundleError(
            400,
            f"{amount} has already been paid on this plan"
            + (f" ({who})" if who else "")
            + ". Deleting it wouldn't return the money — it would only lose the "
            "record of where it went. Refund or resolve "
            f"{'that booking' if len(held) == 1 else 'those bookings'} first, "
            "then delete the plan.",
        )

    try:
        _delete_bundle_cascade(bundle, db)
        db.commit()
    except BundleError:
        # Already a considered refusal — don't bury it in a 500.
        db.rollback()
        raise
    except Exception:
        db.rollback()
        # Logged in full server-side; the client only ever sees a generic
        # message — see the identical fix in user_service.delete_user.
        logger.exception("delete_bundle failed for bundle %s", bundle_id)
        raise BundleError(500, "Something went wrong deleting this plan. Please try again.")


# ── One-off legacy data cleanup ───────────────────────────────────────

# Marker the pre-fix iOS app wrote on events it auto-created for AI bundles.
_LEGACY_AI_EVENT_MARKER = "Bundle from jornAI"

# A booking with any of these payment states is never deleted by the cleanup.
# The same rule the live delete paths enforce — see MONEY_MOVED_STATUSES, which
# this used to be a second, separate copy of.
_PROTECTED_PAYMENT_STATUSES = MONEY_MOVED_STATUSES


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
