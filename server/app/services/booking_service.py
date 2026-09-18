"""Business logic for bookings: create, status update, fetch, check-in."""

import logging
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import case, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import ESCROW_ENABLED
from app.db.models import Booking, Bundle, Event, User, Vendor, Service
from app.models.schemas import BookingStatus, PaymentStatus, RejectionReason
from app.utils.location import calculate_distance_miles
from app.utils.notifications import notify_booking_status_change, notify_check_in

logger = logging.getLogger(__name__)

# ── Exceptions ────────────────────────────────────────────────────────

class BookingError(Exception):
    """Raised when a booking operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ── Availability / conflict detection ─────────────────────────────────

# A vendor is "locked" for a date once a booking is approved or paid — they're
# committed to that event and cannot take another on an overlapping date. A
# pending request is only a lead and does NOT lock the vendor.
LOCKED_BOOKING_STATUSES = (
    BookingStatus.APPROVED.value,
    BookingStatus.PAYMENT_CONFIRMED.value,
)


def _minutes(hhmm: str | None) -> int | None:
    """"18:30" → 1110. None for a missing or unreadable time, including "TBD"."""
    if not hhmm:
        return None
    try:
        hours, minutes = str(hhmm).strip().split(":")[:2]
        h, m = int(hours), int(minutes)
    except (ValueError, TypeError):
        return None
    return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None


def _window(time_start: str | None, time_end: str | None) -> tuple[int, int] | None:
    """A booking's hours as minutes past midnight, or None if either is unknown.

    An end at or before the start has crossed midnight, so it's carried into the
    next day rather than read as a negative span — the same reading the pricing
    arithmetic uses.
    """
    start = _minutes(time_start)
    end = _minutes(time_end)
    if start is None or end is None:
        return None
    if end <= start:
        end += 24 * 60
    return start, end


def _same_single_day(a_start: str, a_end: str | None, b_start: str, b_end: str | None) -> bool:
    """Both bookings run for one day, and it's the same day."""
    return a_end in (None, "", a_start) and b_end in (None, "", b_start) and a_start == b_start


def booking_blocks(
    existing: Booking,
    *,
    date_iso: str,
    date_end: str | None,
    time_start: str | None,
    time_end: str | None,
) -> bool:
    """Would *existing* stop a job running over the given dates and hours?

    Assumes the dates already overlap — the caller's query establishes that.
    This decides the rest: whole days, unless both sides are single-day and on
    the same day, in which case the hours settle it.

    Shared with the bundle builder, which asks the same question of a whole
    vendor list before proposing them. Two copies of this would let the builder
    hide vendors who could take the job, or propose ones who couldn't.
    """
    if not _same_single_day(date_iso, date_end, existing.date_iso, existing.date_end):
        return True
    requested = _window(time_start, time_end)
    booked = _window(existing.time_start, existing.time_end)
    if requested is None or booked is None:
        return True
    return requested[0] < booked[1] and booked[0] < requested[1]


def vendor_has_conflicting_booking(
    *,
    vendor_id: str,
    date_iso: str | None,
    date_end: str | None,
    db: Session,
    time_start: str | None = None,
    time_end: str | None = None,
    exclude_booking_id: str | None = None,
) -> Booking | None:
    """Return an existing locked (approved/paid) booking for *vendor_id* that
    clashes with ``[date_iso, date_end]``, or ``None`` if the vendor is free.

    Dates decide it, except when both bookings are single-day and fall on the
    same day — then the hours do. A photographer who shoots a morning ceremony
    is not thereby unavailable for an evening reception, and treating the
    calendar day as the unit of booking turned down work nobody had a reason to
    turn down.

    Both bookings must say when they run for the hours to settle anything. An
    unknown or TBD time can't be shown not to overlap, so it stays a conflict —
    the safe reading, since the alternative books a vendor twice.

    Multi-day bookings are still whole days: a Friday-to-Sunday hire occupies
    the Saturday completely, and there are no hours on the booking that would
    say otherwise.

    Touching hours count as free. A booking ending at 14:00 and one starting at
    14:00 don't overlap. Whether a vendor can cross town in no time is their
    judgement to make; they're the one accepting.

    Single-day bookings store a null ``date_end`` — treated as ending on
    ``date_iso``. A TBD/missing date can't conflict (nothing to compare).
    """
    if not date_iso or date_iso == "TBD":
        return None
    req_start = date_iso
    req_end = date_end or date_iso

    existing_end = case(
        (Booking.date_end.isnot(None), Booking.date_end),
        else_=Booking.date_iso,
    )
    query = db.query(Booking).filter(
        Booking.vendor_id == vendor_id,
        Booking.status.in_(LOCKED_BOOKING_STATUSES),
        Booking.date_iso <= req_end,
        existing_end >= req_start,
    )
    if exclude_booking_id:
        query = query.filter(Booking.booking_id != exclude_booking_id)

    # The query answers "which days collide"; booking_blocks settles the hours,
    # where a null date_end and a midnight-crossing window are easier to read
    # than in SQL, and the candidate set is a handful of rows at most.
    for existing in query.all():
        if booking_blocks(
            existing,
            date_iso=req_start,
            date_end=date_end,
            time_start=time_start,
            time_end=time_end,
        ):
            return existing
    return None


# ── Helpers ───────────────────────────────────────────────────────────

def _get_booking_parties(db: Session, booking: Booking):
    """Return (client_user, vendor_obj, vendor_user, service) for a booking."""
    client = db.query(User).filter(User.user_id == booking.user_id).first()
    vendor_obj = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    vendor_user = (
        db.query(User).filter(User.user_id == vendor_obj.user_id).first()
        if vendor_obj
        else None
    )
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    return client, vendor_obj, vendor_user, service


def _dispatch_status_notification(
    status: str,
    booking: Booking,
    client: User | None,
    vendor_user: User | None,
    service: Service | None,
    db: Session,
    event_name: str = "Event",
) -> dict:
    """Send push notifications for a booking status change (to all devices)."""
    result = notify_booking_status_change(
        status=status,
        booking_id=booking.booking_id,
        event_name=event_name,
        service_name=service.name if service else "Service",
        client_name=f"{client.f_name} {client.l_name}" if client else "Client",
        vendor_name=f"{vendor_user.f_name} {vendor_user.l_name}" if vendor_user else "Vendor",
        client_user=client,
        vendor_user=vendor_user,
        db=db,
    )
    logger.info("Booking %s status→%s notification: %s", booking.booking_id, status, result)
    return result


# ── Response serialization ────────────────────────────────────────────

def _booking_dict(booking: Booking, db: Session) -> dict:
    """Return a booking as a dict, including negotiation preference flags from both parties."""
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    client = db.query(User).filter(User.user_id == booking.user_id).first()
    vendor_user = db.query(User).filter(User.user_id == vendor.user_id).first() if vendor else None
    # Bookings link to events via their bundle — surface event_id/event_name so the
    # client's event-detail view can match bookings and show the real event name.
    bundle = db.query(Bundle).filter(Bundle.bundle_id == booking.bundle_id).first() if booking.bundle_id else None
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    # Effective total (cents) via the single resolver: stored amount, else a
    # recomputed rate x quantity estimate, else the flat price for event-priced
    # services. None means rate-priced with an unknown quantity — the price is
    # pending the guest count / dates, so the UI shows the rate, not a total.
    total_cents = resolve_total_cents(booking, service)
    # The point check-in will actually be measured against — the same call
    # check_in makes, so a client can gate its button on it and never offer an
    # action the server has to refuse.
    checkin_lat, checkin_lng = checkin_anchor(booking, db)
    # Whether the client may send this vendor their check-in email again, from
    # the same function the endpoint enforces — so the button and the call agree
    # by construction, as with the anchor above. Imported here rather than at
    # the top: reminder_service reads checkin_anchor from this module.
    from app.services.reminder_service import last_reminder_at, resend_state

    resend = resend_state(booking, db)
    last_reminded = last_reminder_at(booking)
    return {
        "booking_id": booking.booking_id,
        "user_id": booking.user_id,
        "vendor_id": booking.vendor_id,
        "client_name": f"{client.f_name} {client.l_name}".strip() if client else None,
        "vendor_name": f"{vendor_user.f_name} {vendor_user.l_name}".strip() if vendor_user else None,
        "service_id": booking.service_id,
        "service_name": service.name if service else None,
        "service_category": service.category if service else None,
        "service_subcategory": service.subcategory if service else None,
        # Effective price: the resolved total when known, else the listed rate.
        "price": (total_cents / 100) if total_cents is not None else (service.price if service else 0.0),
        # The service's pricing unit (hour/day/event/person) and whether the
        # total is still pending a quantity, so the client can show "$X /person"
        # vs a computed total instead of a rate masquerading as a total.
        "price_unit": service.price_unit if service else None,
        "price_pending_quantity": total_cents is None,
        "guest_count": booking.guest_count,
        "performer_count": booking.performer_count,
        # Denormalized from the service so a client can run its own gap-check
        # (bookingGaps()) against the booking alone — see plan_readiness.booking_gaps.
        "require_guest_count": bool(service.require_guest_count) if service else False,
        "require_performer_count": bool(service.require_performer_count) if service else False,
        # What the client said when requesting this — shown to the vendor
        # alongside the request, before they accept/decline.
        "client_note": booking.client_note,
        "bundle_id": booking.bundle_id,
        "event_id": bundle.event_id if bundle else None,
        "event_name": bundle.event_name if bundle else None,
        "date_iso": booking.date_iso,
        "date_end": booking.date_end,
        "time_start": booking.time_start,
        "time_end": booking.time_end,
        "location": booking.location,
        "venue_latitude": booking.venue_latitude,
        "venue_longitude": booking.venue_longitude,
        "checkin_latitude": checkin_lat,
        "checkin_longitude": checkin_lng,
        # The venue's own clock, as an IANA zone name. Published so a client can
        # ask "has the event happened yet" and get the answer this server gives
        # — that comparison is the escrow gate, and a client computing it in the
        # browser's timezone reached a different day from the one enforced here.
        # Null when the address and pin can't place it; the caller then falls
        # back to local, as this does to UTC.
        "timezone": _zone_name(booking),
        # A date change the client has proposed and this vendor owes an answer
        # on. Null when there is none, which is the ordinary case.
        "change_request": _open_change_request(booking, db),
        # Which side owes the next move on an open price negotiation — lets a
        # client/vendor task list surface "review this offer" only for the
        # party who can actually act on it, instead of for whoever last sent
        # the number that's still sitting there unanswered.
        "negotiation_awaiting_role": _negotiation_awaiting_role(booking, db),
        "status": booking.status,
        # Which of the three real events "rejected" covers — null on a row
        # written before this field existed, or on a client-initiated
        # cancellation (see cancelled_at instead). See RejectionReason.
        "rejected_reason": booking.rejected_reason,
        "payment_status": booking.payment_status,
        # The single source of truth for "can anything further happen to this
        # booking" — was being re-derived independently on the frontend
        # (planning.ts's isDeadBooking) from a hand-mirrored copy of these
        # same two constants. Compute it once, here, instead.
        "is_dead": (
            booking.status in _DEAD_BOOKING_STATUSES
            or (booking.payment_status or PaymentStatus.UNPAID.value) in _DEAD_VENUE_PAYMENT_STATUSES
        ),
        # "stripe" (protected) or "manual" (paid directly, Venmo/Zelle) —
        # snapshotted at send time. Null predates this feature; treated as
        # "stripe" everywhere it's read.
        "payment_method": booking.payment_method,
        "amount_cents": booking.amount_cents,
        "currency": booking.currency,
        "client_checked_in_at": booking.client_checked_in_at,
        "vendor_checked_in_at": booking.vendor_checked_in_at,
        "can_resend_checkin": resend["can_resend"],
        "resend_checkin_reason": resend["reason"],
        # When this vendor was last emailed about checking in, either kind. Lets
        # the client see that a nudge landed rather than a button that vanishes
        # into a cooldown and looks broken.
        "checkin_reminded_at": last_reminded.isoformat() if last_reminded else None,
        "confirmed_at": booking.confirmed_at,
        "paid_at": booking.paid_at,
        "funds_released_at": booking.funds_released_at,
        "customer_confirmed_at": booking.customer_confirmed_at,
        "vendor_confirmed_at": booking.vendor_confirmed_at,
        # Price negotiation is now a per-service toggle (not vendor-wide). Both
        # the legacy key and an explicit `negotiable` key carry the service flag.
        "vendor_open_to_price_negotiation": service.negotiable if service else False,
        "negotiable": service.negotiable if service else False,
        "vendor_open_to_location_negotiation": vendor.open_to_location_negotiation if vendor else False,
        "client_open_to_price_negotiation": client.open_to_price_negotiation if client else False,
        "client_flexible_on_location": client.flexible_on_location if client else False,
        # Guest/contract booking fields (docs/DECISIONS.md #13) — present on
        # every booking so a vendor's general list/pipeline view can derive
        # stage without a separate per-booking fetch. All null on an
        # ordinary authenticated booking.
        "is_guest_booking": booking.user_id is None,
        "guest_name": booking.guest_name,
        "guest_email": booking.guest_email,
        "guest_phone": booking.guest_phone,
        "contract_token": booking.contract_token,
        "deposit_percent": booking.deposit_percent,
        "deposit_amount_cents": booking.deposit_amount_cents,
        "cancellation_window_hours": booking.cancellation_window_hours,
        "overtime_rate_cents": booking.overtime_rate_cents,
        "addon_rate_cents": booking.addon_rate_cents,
        "contract_terms": booking.contract_terms,
        "signer_name": booking.signer_name,
        "signed_at": booking.signed_at.isoformat() if booking.signed_at else None,
        "deposit_marked_paid_at": booking.deposit_marked_paid_at.isoformat() if booking.deposit_marked_paid_at else None,
        "deposit_confirmed_received_at": booking.deposit_confirmed_received_at.isoformat() if booking.deposit_confirmed_received_at else None,
    }


# ── Service functions ─────────────────────────────────────────────────

# A venue booking stops anchoring the event once it's in one of these states.
_DEAD_BOOKING_STATUSES = ("rejected", "cancelled")
_DEAD_VENUE_PAYMENT_STATUSES = (PaymentStatus.REFUNDED.value,)


def _live_venue_booking(
    bundle_id: str | None,
    db: Session,
    *,
    bookings: list[Booking] | None = None,
    services: dict[str, Service] | None = None,
) -> tuple[Booking, Service] | None:
    """The bundle's live venue booking + its service, or None.

    "Live" = a venue-category service with GPS coords, whose booking hasn't been
    rejected/cancelled or refunded. This is the source of truth for whether the
    event currently has a venue — computed from the bookings themselves, so it's
    always correct without relying on a denormalized cache.

    ``bookings``/``services`` let a caller that has already loaded them in bulk
    skip the queries. Passed in rather than reimplemented, so a batched screen
    and a single one can't come to different conclusions about whether a plan
    has a venue.
    """
    if not bundle_id:
        return None
    if bookings is None:
        bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    if services is None:
        service_ids = [b.service_id for b in bookings if b.service_id]
        services = (
            {s.service_id: s for s in db.query(Service).filter(Service.service_id.in_(service_ids)).all()}
            if service_ids else {}
        )
    for b in bookings:
        s = services.get(b.service_id)
        if (
            s is not None
            and s.category == "venue"
            and s.venue_latitude is not None
            and s.venue_longitude is not None
            and b.status not in _DEAD_BOOKING_STATUSES
            and (b.payment_status or PaymentStatus.UNPAID.value) not in _DEAD_VENUE_PAYMENT_STATUSES
        ):
            return b, s
    return None


#: Tells "the caller didn't supply this" apart from "the caller supplied None",
#: which for a bundle's event is a real and different answer.
_NOT_GIVEN = object()


def checkin_anchor(
    booking: Booking,
    db: Session,
    *,
    bundle_bookings: list[Booking] | None = None,
    services: dict[str, Service] | None = None,
    event=_NOT_GIVEN,
) -> tuple[float | None, float | None]:
    """Where this booking's check-in is measured from, or (None, None).

    The one definition of that, used both to enforce check-in and to tell the
    clients what it will enforce — so a button is never offered for a call that
    must fail, or withheld from one that would succeed.

    In order: the bundle's live venue booking, which is the source of truth and
    must keep blocking once that venue is removed or refunded; then, for a
    booking with no bundle, its own pin; then the address the client typed on
    the event, for a plan held somewhere they arranged themselves.

    Deliberately not the coordinates mirrored onto the booking row. Those are a
    cache that sync_event_venue refreshes, and reading them here would let the
    answer drift from the one check_in gives.

    The keyword arguments are for a caller that has already loaded a bundle's
    bookings, services and event in bulk — a screen listing many plans, which
    would otherwise pay two queries per plan for an answer it is holding. Every
    rule still lives here; only the reads are skipped.
    """
    live_venue = _live_venue_booking(
        booking.bundle_id, db, bookings=bundle_bookings, services=services
    )
    if live_venue:
        return live_venue[1].venue_latitude, live_venue[1].venue_longitude
    if not booking.bundle_id:
        return booking.venue_latitude, booking.venue_longitude
    if event is not _NOT_GIVEN:
        return (event.address_latitude, event.address_longitude) if event else (None, None)
    return _event_address_pin(booking.bundle_id, db)


def _event_address_pin(
    bundle_id: str | None, db: Session
) -> tuple[float | None, float | None]:
    """The coordinates of the address the client typed, via the bundle's event.

    Untouched by sync_event_venue, so a venue coming and going doesn't disturb
    it. Returns (None, None) when there's no event or it was never geocoded.
    """
    if not bundle_id:
        return None, None
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle or not bundle.event_id:
        return None, None
    event = db.query(Event).filter(Event.event_id == bundle.event_id).first()
    if not event:
        return None, None
    return event.address_latitude, event.address_longitude


def sync_event_venue(bundle_id: str | None, db: Session) -> None:
    """Refresh the denormalized venue anchor from the bundle's live venue booking.

    The live venue booking (see _live_venue_booking) is the source of truth; this
    keeps the convenience copies in step with it: it writes the venue's coords/
    address onto the linked Event and mirrors the coords onto the traveling
    vendors' bookings so the client UI can show them. When there's NO live venue —
    removed, rejected, or refunded — it CLEARS those copies so a venue that's gone
    can't leave the others displaying a stale check-in target (dependency
    orphaning). Runs on every booking change; check-in re-derives the anchor
    directly, so this cache never gates payment.

    Does NOT commit — the caller owns the transaction.
    """
    if not bundle_id:
        return
    bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    if not bookings:
        return

    live = _live_venue_booking(bundle_id, db)
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    event = (
        db.query(Event).filter(Event.event_id == bundle.event_id).first()
        if bundle and bundle.event_id else None
    )

    if live:
        venue_booking, venue_service = live
        lat, lng = venue_service.venue_latitude, venue_service.venue_longitude
        if event:
            event.venue_latitude = lat
            event.venue_longitude = lng
            if venue_service.location:
                event.location = venue_service.location
        # The venue booking is a booking AT the venue, so its own location is
        # the venue's address. The builder writes the event's city onto every
        # booking it creates, including this one — which left the venue booking
        # saying "Los Angeles, CA" while its service said "1200 Bel Air Rd, Los
        # Angeles, CA 90077". The client reads the venue booking to prefill the
        # event address, so it got a city and no street.
        if venue_service.location:
            venue_booking.location = venue_service.location

        # Mirror the coords onto the traveling vendors' bookings; the venue's
        # own booking already carries its service coords.
        for b in bookings:
            if b.booking_id == venue_booking.booking_id:
                continue
            b.venue_latitude = lat
            b.venue_longitude = lng
            if venue_service.location and not b.location:
                b.location = venue_service.location
    else:
        if event:
            event.venue_latitude = None
            event.venue_longitude = None
        for b in bookings:
            b.venue_latitude = None
            b.venue_longitude = None


# ── Pricing estimate (rate x quantity) ───────────────────────────────

def _normalize_unit(price_unit: str | None) -> str | None:
    """Map a service's price_unit to hour/day/event/person. Tolerates legacy
    display strings ("Per Hour") and short codes ("hour")."""
    if not price_unit:
        return None
    u = price_unit.strip().lower()
    if u.startswith("per "):
        u = u[4:].strip()
    if u.startswith("hour"):
        return "hour"
    if u.startswith("day"):
        return "day"
    if u.startswith("event"):
        return "event"
    if u.startswith("person") or u in ("head", "plate", "guest", "pax"):
        return "person"
    if u.startswith(("performer", "dancer", "entertainer")):
        return "performer"
    return None


def canonical_price_unit(value: str | None) -> str | None:
    """The one of the four this input means, or a refusal.

    Read forgivingly, stored canonical. Clients have been sending "Per Hour" and
    "per head" for years and there is no reason to break them — but what lands
    in the column is one of person/hour/day/event and nothing else, so nothing
    downstream has to have an opinion about a string again.

    None and empty stay None: a service that never said how it charges is
    priced flat, which is what the absence has always meant.

    Anything unrecognisable is refused rather than quietly stored. It used to be
    accepted and priced flat, which meant a vendor could type a word and get a
    total two hundred times smaller than they meant.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    unit = _normalize_unit(text)
    if unit is None:
        raise ValueError(
            f"'{value}' isn't a pricing unit we can charge against. "
            "Use one of: person, hour, day, event, performer."
        )
    return unit


def _parse_clock(value: str | None) -> float | None:
    """Parse a clock string ("5:00 PM", "17:00", "5 pm") to fractional hours
    (0..24). Returns None for vague values ("evening", "TBD", "")."""
    if not value:
        return None
    s = value.strip().lower()
    if not s or s in ("tbd", "n/a"):
        return None
    import re
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    ampm = m.group(3)
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 24 or minute >= 60:
        return None
    return hour + minute / 60.0


def _parse_date(value: str | None):
    """Parse an ISO-ish date (YYYY-MM-DD, optionally with time) to a date. None
    for vague/unparseable values."""
    if not value:
        return None
    s = value.strip()
    if not s or s.upper() == "TBD":
        return None
    from datetime import date
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def estimate_amount_cents(
    service: Service | None,
    *,
    guest_count: int | None = None,
    performer_count: int | None = None,
    date_iso: str | None = None,
    date_end: str | None = None,
    time_start: str | None = None,
    time_end: str | None = None,
) -> int | None:
    """Estimate a booking's total in cents = rate x quantity, per the service's
    price_unit. Returns None when the quantity can't be determined — the caller
    then leaves amount_cents nil and the booking prices at the flat rate.

    person -> guest_count; performer -> performer_count; day -> days in the
    date range; hour -> hours in the event time window; event/unknown/missing-
    data -> None (flat rate).
    """
    if not service:
        return None
    unit = _normalize_unit(service.price_unit)
    rate = service.price or 0.0
    if unit is None or rate <= 0:
        return None

    if unit == "person":
        if guest_count and guest_count > 0:
            return round(rate * guest_count * 100)
        return None

    if unit == "performer":
        if performer_count and performer_count > 0:
            return round(rate * performer_count * 100)
        return None

    if unit == "day":
        start = _parse_date(date_iso)
        if not start:
            return None
        end = _parse_date(date_end) or start
        days = max((end - start).days + 1, 1)
        return round(rate * days * 100)

    if unit == "hour":
        start = _parse_clock(time_start)
        end = _parse_clock(time_end)
        if start is None or end is None:
            return None
        hours = end - start
        if hours <= 0:
            hours += 24  # crosses midnight (e.g. 8 PM - 1 AM)
        if hours <= 0 or hours > 24:
            return None
        return round(rate * hours * 100)

    # "event" -> flat rate: leave amount nil so it prices at service.price.
    return None


def resolve_total_cents(booking: Booking, service: Service | None) -> int | None:
    """The amount to charge for a booking, in cents — the single source of truth
    for display and payment.

    Order of resolution:
      1. An explicit ``amount_cents`` (a stored estimate or a negotiated price) wins.
      2. Otherwise recompute the rate x quantity estimate from the booking's own
         persisted fields (guest_count / dates / time window).
      3. Otherwise, for an ``event``-priced (or unpriced) service the flat
         ``service.price`` IS the total, so charge that.

    Returns ``None`` only when the service is rate-priced (per person/day/hour)
    and the quantity still can't be determined. Callers must then obtain the
    quantity before charging rather than bill the bare per-unit rate as a total.
    """
    if booking.amount_cents is not None:
        return booking.amount_cents
    if not service:
        return None
    est = estimate_amount_cents(
        service,
        guest_count=booking.guest_count,
        performer_count=booking.performer_count,
        date_iso=booking.date_iso,
        date_end=booking.date_end,
        time_start=booking.time_start,
        time_end=booking.time_end,
    )
    if est is not None:
        return est
    unit = _normalize_unit(service.price_unit)
    if unit in (None, "event"):
        # Flat price is a genuine total for event/unpriced services.
        return round((service.price or 0.0) * 100)
    # Rate-priced (person/day/hour) but quantity unknown → indeterminate.
    return None


def pending_quantity_reason(service: Service | None) -> str:
    """Human phrase naming the quantity a rate-priced service still needs before
    its total can be computed — used in the pre-payment guard's error message."""
    unit = _normalize_unit(service.price_unit) if service else None
    return {
        "person": "the guest count",
        "performer": "the performer count",
        "day": "the event dates",
        "hour": "the start and end times",
    }.get(unit, "the event details")


def create_booking(
    *,
    user_id: str,
    service_id: str,
    event_name: str,
    time_start: str,
    time_end: str,
    location: str,
    date_iso: str,
    venue_latitude: float | None,
    venue_longitude: float | None,
    bundle_id: str | None = None,
    date_end: str | None = None,
    guest_count: int | None = None,
    performer_count: int | None = None,
    client_note: str | None = None,
    db: Session,
) -> dict:
    """Create a new booking and notify the vendor.

    If bundle_id is provided the booking is added to that bundle (event_name
    on the bundle is updated if not already set). Otherwise a new single-booking
    bundle is auto-created to store the event_name.
    """
    service = db.query(Service).filter(Service.service_id == service_id).first()
    if not service:
        raise BookingError(404, "Service not found")

    # Same guard as update_booking — see its comment. A booking created
    # straight from a service page (book/page.tsx) supplies date_iso up
    # front, so this is that flow's equivalent choke point.
    from app.services.plan_readiness import is_in_the_past

    if is_in_the_past(date_iso):
        raise BookingError(400, "That date has already passed — check the year.")

    now = datetime.now(timezone.utc)

    if bundle_id:
        bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
        if not bundle:
            raise BookingError(404, "Bundle not found")
        if bundle.user_id != user_id:
            raise BookingError(403, "You do not own this bundle")
        if not bundle.event_name:
            bundle.event_name = event_name
            bundle.updated_at = now
    else:
        bundle = Bundle(
            user_id=user_id,
            name=f"{event_name} Bundle",
            event_name=event_name,
            status="draft",
            created_at=now,
            updated_at=now,
        )
        db.add(bundle)
        db.flush()
        bundle_id = bundle.bundle_id

    # Joining a plan that has already gone out means this vendor is told the
    # moment the row exists — so it has to be complete now, the same way a plan
    # has to be complete before it can be sent. There is no draft to fix it in
    # afterwards, and once the request is out it can't be edited.
    if bundle.status != "draft":
        from app.services.plan_readiness import booking_gaps, describe_gaps

        proposed = Booking(
            date_iso=date_iso, date_end=date_end, guest_count=guest_count,
            performer_count=performer_count,
            time_start=time_start, time_end=time_end, location=location,
        )
        gaps = booking_gaps(proposed, service)
        if gaps:
            raise BookingError(
                400,
                f"This plan is already with your vendors, so {service.name} needs "
                f"{describe_gaps(gaps)} before it can be added — it goes out as "
                "soon as it's booked, and can't be changed afterwards.",
            )

    def _existing_live_booking() -> Booking | None:
        """The same slot (bundle + vendor + service + date) already booked and
        not rejected/cancelled."""
        return (
            db.query(Booking)
            .filter(
                Booking.bundle_id == bundle_id,
                Booking.vendor_id == service.vendor_id,
                Booking.service_id == service_id,
                Booking.date_iso == date_iso,
                Booking.status.notin_(("rejected", "cancelled")),
            )
            .first()
        )

    def _idempotent_response(existing: Booking) -> dict:
        return {
            "message": "Booking already exists for this vendor, service, and date",
            "booking_id": existing.booking_id,
            "bundle_id": existing.bundle_id,
            "status": existing.status,
            "notification": None,
        }

    # Duplicate guard: client retries, double-taps, or a second code path
    # re-booking the same slot get the existing booking back instead of a
    # duplicate (the partial unique index backstops the remaining race).
    existing = _existing_live_booking()
    if existing:
        return _idempotent_response(existing)

    # Snapshot the vendor's payment track now, not read live later — a vendor
    # switching tracks after this booking exists shouldn't change the deal
    # a client already agreed to. With escrow disabled, force manual
    # regardless of what's stored on the vendor row (see docs/DECISIONS.md
    # #12) — this is what actually keeps new bookings off Stripe, independent
    # of whether every vendor row has been updated yet.
    vendor = db.query(Vendor).filter(Vendor.vendor_id == service.vendor_id).first()
    booking_payment_method = "manual" if not ESCROW_ENABLED else (vendor.payment_method if vendor else None)

    booking = Booking(
        booking_id=str(uuid.uuid4()),
        user_id=user_id,
        vendor_id=service.vendor_id,
        service_id=service_id,
        time_start=time_start,
        time_end=time_end,
        location=location,
        date_iso=date_iso,
        date_end=date_end,
        guest_count=guest_count,
        performer_count=performer_count,
        client_note=client_note,
        venue_latitude=venue_latitude,
        venue_longitude=venue_longitude,
        status=BookingStatus.PENDING.value,
        bundle_id=bundle_id,
        payment_method=booking_payment_method,
    )
    # Estimate the total = rate x quantity from everything the booking carries.
    # When the quantity is still unknown (e.g. a per-person service with no guest
    # count), amount_cents stays nil and the price is resolved/guarded later —
    # never billed as the bare per-unit rate (see resolve_total_cents + checkout).
    est = estimate_amount_cents(
        service,
        guest_count=guest_count,
        performer_count=performer_count,
        date_iso=date_iso,
        date_end=date_end,
        time_start=time_start,
        time_end=time_end,
    )
    if est is not None:
        booking.amount_cents = est
    db.add(booking)
    try:
        db.commit()
    except IntegrityError:
        # Lost a create race — the unique index caught it. Return the winner.
        db.rollback()
        existing = _existing_live_booking()
        if existing:
            return _idempotent_response(existing)
        raise
    db.refresh(booking)

    # Share the bundle's venue location/coords across its bookings (so traveling
    # vendors can check in) and make sure the bundle is backed by a real Event so
    # it shows up under the client's My Events / Event Portfolio.
    try:
        from app.services.bundle_service import _ensure_bundle_event
        _bundle = db.query(Bundle).filter(Bundle.bundle_id == booking.bundle_id).first()
        if _bundle:
            _ensure_bundle_event(_bundle, db)
        # Anchor the event's venue (and mirror onto the bookings) from the live
        # venue booking — after the event exists so it's written there too.
        sync_event_venue(booking.bundle_id, db)
        db.commit()
    except Exception as exc:
        logger.warning("create_booking: failed to ensure bundle event: %s", exc)
        db.rollback()

    # A booking added to a draft plan has not been sent to anybody. The client is
    # still assembling it — adding a service from the marketplace, or swapping
    # one out, which is "book the replacement, then remove the original" and so
    # arrives here too. Telling the vendor at that point asks them to hold a date
    # for a plan that may still have no date, and may be undone thirty seconds
    # later by the swap that created it.
    #
    # select_bundle is what tells them, once the plan is complete and the client
    # presses Send; it notifies every booking still pending, so anything added
    # meanwhile is caught up then.
    #
    # A booking joining a plan that has already gone out is a different case: the
    # vendors are waiting, and this one was refused above unless it arrived
    # complete — so it is notified now, as it always was.
    parent = (
        db.query(Bundle).filter(Bundle.bundle_id == booking.bundle_id).first()
        if booking.bundle_id
        else None
    )
    still_a_draft = parent is not None and parent.status == "draft"

    client, vendor_obj, vendor_user, _ = _get_booking_parties(db, booking)
    if still_a_draft:
        notification = {"skipped": "Draft plan — vendors are told when it's sent."}
    else:
        try:
            notification = _dispatch_status_notification(
                BookingStatus.PENDING.value, booking, client, vendor_user, service, db,
                event_name=event_name,
            )
        except Exception as exc:
            logger.warning("Notification failed for booking %s: %s", booking.booking_id, exc)
            notification = {"error": "Notification unavailable"}

    return {
        "message": "Booking requested successfully",
        "booking_id": booking.booking_id,
        "bundle_id": bundle_id,
        "status": booking.status,
        "notification": notification,
    }


def update_booking_status(
    *,
    booking_id: str,
    caller_user_id: str,
    status: BookingStatus,
    db: Session,
) -> dict:
    """Validate authorization + business rules, then update status."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise BookingError(404, "Booking not found")

    status_str = status.value

    # Derive role server-side — never trust a client-supplied flag.
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    is_vendor = vendor is not None and vendor.user_id == caller_user_id
    is_customer = booking.user_id == caller_user_id

    if not is_vendor and not is_customer:
        raise BookingError(403, "You are not a party to this booking")

    # Business rules
    _negotiable = {BookingStatus.PENDING.value, BookingStatus.NEGOTIATION_ONGOING.value}

    if status_str == BookingStatus.NEGOTIATION_ONGOING.value:
        if booking.status not in _negotiable:
            raise BookingError(
                400, f"Cannot move to negotiation from {booking.status}"
            )

    if status_str in [BookingStatus.APPROVED.value, BookingStatus.REJECTED.value]:
        if not is_vendor:
            raise BookingError(403, "Only vendors can approve or reject a booking")

        # Declining an unanswered request, or pulling out of one already
        # accepted. The second is new: a vendor whose circumstances changed had
        # no way out of a booking at all, so the only honest options were to
        # tell the client in the chat and hope, or not turn up.
        #
        # 'paid' is the one money-moved state this is allowed from — see the
        # refund below. Every other money-moved state stays blocked
        # (MONEY_MOVED_STATUSES minus 'paid'; the delete paths still refuse on
        # all of them, including 'processing', because money in flight is the
        # worst moment to walk away): a Stripe charge still in flight, funds
        # already released to the vendor, or a booking already refunded/
        # disputed/cancelled isn't a plain "vendor changed their mind."
        cancelling = (
            status_str == BookingStatus.REJECTED.value
            and booking.status == BookingStatus.APPROVED.value
        )
        _refund_client_on_cancel = False
        if cancelling:
            from app.services.bundle_service import _money_has_moved

            if booking.payment_status == PaymentStatus.PAID.value:
                _refund_client_on_cancel = True
            elif _money_has_moved(booking):
                raise BookingError(
                    400,
                    "This booking has already been paid for, so it can't be "
                    "cancelled here. Ask the client to request a refund, or "
                    "raise a problem on it if something has gone wrong.",
                )
        elif booking.status not in _negotiable:
            raise BookingError(
                400, f"Cannot change status from {booking.status} to {status_str}"
            )
        # A request nobody has sent isn't the vendor's to answer. The list they
        # work from excludes these now (see get_vendor_bookings), so this is the
        # backstop — and it matters, because agreeing to a draft means agreeing
        # to a date and a place the client hasn't decided on.
        parent = (
            db.query(Bundle).filter(Bundle.bundle_id == booking.bundle_id).first()
            if booking.bundle_id
            else None
        )
        if parent is not None and parent.status == "draft":
            raise BookingError(
                400,
                "This request hasn't been sent yet — the client is still putting "
                "their plan together. You'll be asked when they send it.",
            )

    # A vendor can't approve two bookings that collide. Checked only on approval
    # (a pending request is just a lead); checkout re-checks to catch the race
    # between approval and payment.
    if status_str == BookingStatus.APPROVED.value:
        conflict = vendor_has_conflicting_booking(
            vendor_id=booking.vendor_id,
            date_iso=booking.date_iso,
            date_end=booking.date_end,
            time_start=booking.time_start,
            time_end=booking.time_end,
            db=db,
            exclude_booking_id=booking.booking_id,
        )
        if conflict:
            # Name the hours that clash. "You're busy that day" is answerable
            # with "no I'm not, that one finishes at two" — and now it might be
            # right, so the refusal has to say which booking and when.
            when = (
                f" ({conflict.time_start}–{conflict.time_end})"
                if conflict.time_start and conflict.time_end
                else ""
            )
            raise BookingError(
                409,
                f"That clashes with a booking you've already confirmed{when}. "
                "You can take another job the same day as long as the hours "
                "don't overlap.",
            )

    # A vendor can't lock in the listed price while a counter-offer is still
    # on the table — that's this exact bug: clicking plain Accept here would
    # silently discard an open Negotiation row with zero warning. Keyed off
    # the Negotiation row itself (not booking.status) since that row is the
    # thing that actually gets thrown away, and it's the source of truth
    # regardless of how booking.status happens to read.
    if status_str == BookingStatus.APPROVED.value:
        from app.db.models import Negotiation

        open_negotiation = (
            db.query(Negotiation)
            .filter(
                Negotiation.booking_id == booking.booking_id,
                Negotiation.status == "open",
            )
            .first()
        )
        if open_negotiation:
            raise BookingError(
                409,
                "There's an open price offer on this booking. Accept, "
                "counter, or decline it in the negotiation first — "
                "approving here would lock in the original price and "
                "throw away the offer.",
            )

    booking.status = status_str
    if status_str == BookingStatus.APPROVED.value:
        booking.confirmed_at = datetime.now(timezone.utc)
    # A rejected venue no longer anchors the event — refresh so its cached coords
    # clear (check-in re-derives regardless, but keep the denormalized copies honest).
    if status_str == BookingStatus.REJECTED.value:
        # `cancelling` (above) already distinguishes a vendor backing out of
        # an approved booking from a plain decline — the only two ways this
        # function itself produces REJECTED.
        booking.rejected_reason = (
            RejectionReason.VENDOR_WITHDREW.value
            if cancelling
            else RejectionReason.VENDOR_DECLINED.value
        )
        # Nothing is waiting on a booking that isn't happening. A live date
        # change would otherwise sit on the client's board forever, waiting on a
        # vendor who has gone — and offer them a refund on a booking that was
        # never paid for.
        from app.db.models import ChangeRequest

        db.query(ChangeRequest).filter(
            ChangeRequest.booking_id == booking.booking_id,
            ChangeRequest.status == "pending",
        ).update(
            {"status": "withdrawn", "resolved_at": datetime.now(timezone.utc)},
            synchronize_session=False,
        )
        db.flush()
        sync_event_venue(booking.bundle_id, db)
    db.commit()
    db.refresh(booking)

    # A vendor pulling out of an accepted, paid booking always owes the
    # client every cent back — no ramp, unlike a client-initiated
    # cancellation (stripe_service.cancel_booking). They're the one breaking
    # the commitment, so they forfeit their share entirely rather than
    # keeping any of it for having held the date.
    if status_str == BookingStatus.REJECTED.value and _refund_client_on_cancel:
        import stripe

        try:
            if not booking.payment_intent_id:
                raise ValueError("No payment intent found for this booking")
            stripe.Refund.create(
                payment_intent=booking.payment_intent_id,
                reason="requested_by_customer",
            )
            booking.payment_status = PaymentStatus.REFUNDED.value
            db.commit()
            db.refresh(booking)
        except Exception as exc:  # noqa: BLE001 — including stripe.StripeError
            logger.error(
                "Vendor-cancel refund failed for booking %s: %s", booking.booking_id, exc
            )
            db.rollback()

    # Accepting is what triggers the charge. The client saved a card when they
    # sent the plan precisely so this moment wouldn't wait on them coming back —
    # a vendor who has just held a date shouldn't then be waiting on a payment
    # that is nobody's next action.
    #
    # Never fatal. An off-session charge can decline for reasons that have
    # nothing to do with the acceptance — a card needing 3-D Secure, an expiry,
    # a limit — and the vendor's answer is not the place to surface that. The
    # booking stays approved and unpaid, which is exactly the state the manual
    # Pay button already handles, so the client is asked in the usual way.
    # A manual-track booking has no card to charge — the client pays the
    # vendor directly via mark_booking_paid/confirm_payment_received instead.
    # payment_method is None for anything created before that track existed,
    # which is always Stripe.
    if ESCROW_ENABLED and status_str == BookingStatus.APPROVED.value and booking.payment_method != "manual":
        try:
            from app.services.stripe_service import CardChargeUnavailable, charge_saved_card

            charge_saved_card(booking_id=booking.booking_id, db=db)
            db.refresh(booking)
        except CardChargeUnavailable as exc:
            logger.info("No automatic charge for booking %s: %s", booking.booking_id, exc)
        except Exception as exc:  # noqa: BLE001 — including stripe.CardError
            logger.warning(
                "Automatic charge failed for booking %s: %s", booking.booking_id, exc
            )
            db.rollback()

    # The vendor's own Google Calendar, if they're connected with write
    # access — same best-effort contract as the notification dispatch just
    # below: never lets a calendar problem fail the status change itself.
    if status_str == BookingStatus.APPROVED.value:
        from app.services.calendar_service import sync_booking_to_calendar

        sync_booking_to_calendar(booking, db)
    elif status_str == BookingStatus.REJECTED.value and booking.google_event_id:
        from app.services.calendar_service import remove_booking_from_calendar

        remove_booking_from_calendar(booking, db)

    client, vendor_obj, vendor_user, service = _get_booking_parties(db, booking)
    _bundle = db.query(Bundle).filter(Bundle.bundle_id == booking.bundle_id).first() if booking.bundle_id else None
    _event_name = (_bundle.event_name if _bundle else None) or "Event"
    try:
        notification = _dispatch_status_notification(
            status_str, booking, client, vendor_user, service, db, event_name=_event_name,
        )
    except Exception as exc:
        logger.warning("Notification failed for booking %s: %s", booking.booking_id, exc)
        notification = {"error": "Notification unavailable"}

    return {
        "message": f"Booking successfully updated to {status_str}",
        "booking_id": booking.booking_id,
        "notification": notification,
    }


def update_booking(
    *,
    booking_id: str,
    caller_user_id: str,
    update_data: dict,
    db: Session,
) -> dict:
    """Update mutable fields on a booking. Only the client can call this,
    and only while the booking is still pending or under negotiation."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise BookingError(404, "Booking not found")
    if booking.user_id != caller_user_id:
        raise BookingError(403, "Only the client who made this booking can update it")

    if booking.status in _DEAD_BOOKING_STATUSES:
        raise BookingError(
            400,
            f"Booking cannot be edited in '{booking.status}' status.",
        )

    # What a vendor was told is what they answered, so once a request is out its
    # details stop being the client's to change. A field that is still *empty*
    # can be filled — that completes the request rather than altering it, and
    # without it a booking that went out short of a headcount could never be
    # paid for.
    #
    # This replaced a status check that allowed anything while a booking was
    # still 'pending'. A pending booking is one sitting in a vendor's inbox
    # being read, which is exactly when a silent edit does the most damage; and
    # the same check forbade filling a gap on an approved one, which is the only
    # way such a booking ever becomes payable.
    from app.services.plan_readiness import is_in_the_past, refuse_locked_changes

    refuse_locked_changes(booking, update_data, db)

    # Same rule the send gate refuses on (plan_readiness.booking_gaps), just
    # enforced where a bad date can actually be typed rather than only where
    # it's finally sent. A mistyped year (e.g. a native date input's segment
    # sticking on "0026") used to save silently — nothing rejected it until
    # Send, and a draft never reaches Send on its own, so it just sat there
    # while the builder's own vendor search quietly failed against it.
    if "date_iso" in update_data and is_in_the_past(update_data["date_iso"]):
        raise BookingError(400, "That date has already passed — check the year.")

    allowed_fields = {"date_iso", "date_end", "guest_count", "performer_count", "time_start", "time_end", "location", "venue_latitude", "venue_longitude"}
    quantity_fields = {"date_iso", "date_end", "guest_count", "performer_count", "time_start", "time_end"}
    touched_quantity = False
    for field, value in update_data.items():
        if field in allowed_fields:
            setattr(booking, field, value)
            if field in quantity_fields:
                touched_quantity = True

    # Re-price when a quantity-affecting field changed, so an edited guest count /
    # date range / time window re-totals the booking. Safe here because updates are
    # only allowed while pending or under negotiation — an accepted negotiation is
    # already 'approved' (and thus not editable), so we never clobber an agreed price.
    # estimate returns None for flat/event-priced or still-unknown quantities, which
    # correctly leaves the total to resolve_total_cents' fallback.
    if touched_quantity:
        service = db.query(Service).filter(Service.service_id == booking.service_id).first()
        booking.amount_cents = estimate_amount_cents(
            service,
            guest_count=booking.guest_count,
            performer_count=booking.performer_count,
            date_iso=booking.date_iso,
            date_end=booking.date_end,
            time_start=booking.time_start,
            time_end=booking.time_end,
        )

    db.commit()
    db.refresh(booking)
    return {
        "booking_id": booking.booking_id,
        "date_iso": booking.date_iso,
        "date_end": booking.date_end,
        "guest_count": booking.guest_count,
        "time_start": booking.time_start,
        "time_end": booking.time_end,
        "location": booking.location,
        "venue_latitude": booking.venue_latitude,
        "venue_longitude": booking.venue_longitude,
        "status": booking.status,
    }


def get_booking(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    """Return a single booking. Caller must be the client or the vendor."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise BookingError(404, "Booking not found")
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    is_vendor = vendor is not None and vendor.user_id == caller_user_id
    is_customer = booking.user_id == caller_user_id
    if not is_vendor and not is_customer:
        raise BookingError(403, "You are not a party to this booking")
    return _booking_dict(booking, db)


def get_user_bookings(*, user_id: str, limit: int = 20, offset: int = 0, db: Session) -> dict:
    """Return a paginated list of bookings created by a client."""
    query = db.query(Booking).filter(Booking.user_id == user_id)
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": [_booking_dict(b, db) for b in items], "total": total, "limit": limit, "offset": offset}


def get_vendor_bookings(*, vendor_id: str, caller_user_id: str, limit: int = 20, offset: int = 0, db: Session) -> dict:
    """Return a paginated list of bookings directed to a vendor.

    Bookings on a plan the client hasn't sent are excluded, because they have
    not been directed at anybody yet.

    create_booking has always withheld the *notification* for a draft — but the
    booking row is created "pending" straight away, and this list is what the
    vendor's dashboard, their "Needs you" badge and their bookings page are all
    built from. So a service a client added from the marketplace while still
    assembling their plan arrived in the vendor's Requests queue with Accept and
    Decline buttons on it. Silence is not the same as absence, and the vendor
    could approve a booking whose date, address and headcount the client had not
    filled in yet — the exact thing the draft is for.

    select_bundle notifies every still-pending booking when the plan is sent, so
    nothing is lost by holding them back: they appear, all at once, at the moment
    the client actually asks.

    A booking with no bundle at all is kept. Nothing is hiding it, and legacy
    rows predate bundles being mandatory.
    """
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise BookingError(404, "Vendor not found")
    if vendor.user_id != caller_user_id:
        raise BookingError(403, "You are not authorised to view these bookings")
    query = (
        db.query(Booking)
        # Outer, so a booking with no bundle survives the join. `NULL != 'draft'`
        # is NULL in SQL rather than true, which is why the null case is spelled
        # out rather than left to the inequality.
        .outerjoin(Bundle, Booking.bundle_id == Bundle.bundle_id)
        .filter(
            Booking.vendor_id == vendor_id,
            or_(Bundle.bundle_id.is_(None), Bundle.status != "draft"),
        )
    )
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": [_booking_dict(b, db) for b in items], "total": total, "limit": limit, "offset": offset}


def _open_change_request(booking: Booking, db: Session) -> dict | None:
    """The date change this vendor still owes an answer on.

    Only the open one: a vendor's list is a list of things to do, and a request
    they already answered isn't one. Expiry is applied on read, so a request
    past its window never appears as still awaiting them.
    """
    from app.services.change_request_service import open_for_booking

    cr = open_for_booking(booking.booking_id, db)
    if not cr:
        return None
    return {
        "change_request_id": cr.change_request_id,
        "date_iso": cr.date_iso,
        "date_end": cr.date_end,
        "time_start": cr.time_start,
        "time_end": cr.time_end,
        "message": cr.message,
        # Set once the vendor has accepted and the client owes approval for a
        # price rise — the vendor's side is done, so it shows as waiting.
        "repriced_amount_cents": cr.repriced_amount_cents,
        "created_at": cr.created_at.isoformat() if cr.created_at else None,
    }


def negotiation_role_from(booking: Booking, neg) -> str | None:
    """Pure half of _negotiation_awaiting_role — given an already-fetched (or
    absent) open Negotiation row, which role owes the next move.

    Whoever is `proposed_by` made the last move, so the other role is the one
    still holding the ball. Split out so a batch caller (bundle_service, which
    must not pay a query per booking) can resolve every booking's negotiation
    in one query and reuse this comparison, rather than reimplementing it.
    """
    if not neg or neg.status != "open":
        return None
    return "vendor" if neg.proposed_by == booking.user_id else "client"


def _negotiation_awaiting_role(booking: Booking, db: Session) -> str | None:
    """Which side of this booking owes the next move on price — "client" or
    "vendor" — or None when there's no live negotiation. See
    negotiation_role_from for the comparison itself; this just fetches.
    """
    if booking.status != "negotiation_ongoing":
        return None
    from app.db.models import Negotiation

    neg = db.query(Negotiation).filter(Negotiation.booking_id == booking.booking_id).first()
    return negotiation_role_from(booking, neg)


def _zone_name(booking: Booking) -> str | None:
    """The IANA zone the celebration's clock runs on, or None if unplaceable."""
    from app.services.timezone_service import zone_name_for

    return zone_name_for(
        location=booking.location,
        latitude=booking.venue_latitude,
        longitude=booking.venue_longitude,
    )


def venue_today(booking: Booking) -> date:
    """Today's date where the celebration is.

    Escrow is gated on whether the event has happened, and "has it happened" is
    a question about the calendar hanging on the wall at the venue, not the
    server's. Reading UTC put a Los Angeles wedding's last day behind us from
    5pm the day before — so the gate opened, and the money could move, while the
    couple were still getting ready.

    Falls back to UTC when the zone can't be resolved, which is the behaviour
    this replaces. A booking with no address and no pin has nothing better to
    offer, and refusing to answer would freeze escrow rather than protect it.
    """
    from app.services.timezone_service import zone_for

    zone = zone_for(
        location=booking.location,
        latitude=booking.venue_latitude,
        longitude=booking.venue_longitude,
    )
    return datetime.now(zone or timezone.utc).date()


def event_confirmable_date(booking: Booking) -> tuple[bool, str | None]:
    """Whether this booking may be confirmed for escrow release yet.
    Returns (ok, error_message).

    Two ways to qualify.

    The scheduled one: the booking's LAST day is over (date_end for a multi-day
    event, else date_iso). Funds are held until the event has taken place, so
    neither party can confirm — and release — before it.

    Over, not merely reached: this compared `today >= end`, which on the morning
    of the wedding is already true. Nothing had happened yet, and both parties
    could settle up for it. Measured in the venue's own timezone (see
    venue_today) the same comparison was true from the previous afternoon.

    And the one that reflects what happened rather than what was booked: the
    event has started and the vendor has checked in at the venue. A booking runs
    to the time it was written down for, and a job frequently doesn't — the DJ
    packs up at eleven, the caterers are done by nine, the photographer leaves
    when the couple does. Holding a client to the end of a three-day window
    before they can settle up with someone who finished on the first afternoon
    makes them wait on a date rather than on the work. A GPS check-in is the
    vendor's own evidence they turned up, so it's a fair thing to release
    against once the day itself has arrived.

    A TBD / unparseable date is NOT confirmable either way: a real date must be
    set first, so an unscheduled event can never release escrow.
    """
    start = _parse_date(booking.date_iso)
    end = _parse_date(booking.date_end) or start
    if end is None:
        return False, (
            "This booking has no scheduled date yet. Set the event date before "
            "confirming — the payment is held until the event has taken place."
        )

    today = venue_today(booking)
    if today > end:
        return True, None

    if start is not None and today >= start and booking.vendor_checked_in_at:
        return True, None

    human = f"{end.strftime('%B')} {end.day}, {end.year}"
    if start is not None and today >= start:
        return False, (
            "You can confirm once your vendor has checked in at the venue, or "
            f"the day after the booking ends on {human}."
        )
    return False, f"You can confirm once the event has taken place, after {human}."


def check_in(
    *,
    booking_id: str,
    caller_user_id: str,
    latitude: float,
    longitude: float,
    db: Session,
) -> dict:
    """Verify GPS proximity and record check-in."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise BookingError(404, "Booking not found")

    # Re-derive the venue anchor from the bundle's live venue booking (source of
    # truth) rather than trusting the coords mirrored onto this booking — so a
    # removed/refunded venue blocks check-in immediately instead of letting a
    # traveling vendor check in (and release funds) against a venue that's gone.
    venue_lat, venue_lng = checkin_anchor(booking, db)

    if venue_lat is None or venue_lng is None:
        raise BookingError(400, "This event has no address on it yet — check-in opens once the plan has a place to be.")

    distance = calculate_distance_miles(latitude, longitude, venue_lat, venue_lng)
    if distance > 0.2:
        raise BookingError(
            400,
            f"You must be at the venue to check in. You are currently {round(distance, 2)} miles away.",
        )

    # Derive role server-side — never trust a client-supplied flag.
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    is_vendor = vendor is not None and vendor.user_id == caller_user_id
    is_customer = booking.user_id == caller_user_id

    if not is_vendor and not is_customer:
        raise BookingError(403, "You are not a party to this booking")

    current_time = datetime.now(timezone.utc).isoformat()

    released = False
    if is_vendor:
        booking.vendor_checked_in_at = current_time
        # The vendor's GPS check-in IS their event-completion confirmation, and
        # nothing about that depends on whether the client has paid yet. It used
        # to: a vendor who checked in before payment cleared was recorded as
        # present and never as confirmed, and no later event revisited it — so
        # their client confirmed, waited on a second confirmation that could not
        # arrive, and the money stayed put. Turning up is the thing being
        # attested; when it gets paid for is somebody else's timing.
        #
        # Confirming early can't release early: release also needs the CUSTOMER's
        # confirmation, which is gated on the event having started (see
        # event_confirmable_date), and needs the money to be there at all.
        if not booking.vendor_confirmed_at:
            booking.vendor_confirmed_at = datetime.now(timezone.utc)
            # Both parties in, and money to move. The payment check moved here
            # from the line above: it belongs to releasing funds, not to whether
            # somebody turned up.
            if booking.customer_confirmed_at and booking.payment_status == PaymentStatus.PAID.value:
                # Best-effort: a Stripe failure must not fail the check-in the
                # vendor just made.
                try:
                    from app.services.stripe_service import _release_funds
                    _release_funds(booking, db)
                    released = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "check_in: fund release failed for booking %s: %s",
                        booking.booking_id, exc,
                    )
    else:
        booking.client_checked_in_at = current_time

    db.commit()

    # Notify the other party (on all their devices)
    client, vendor_obj, vendor_user, _ = _get_booking_parties(db, booking)
    recipient_user = client if is_vendor else vendor_user
    _bundle = db.query(Bundle).filter(Bundle.bundle_id == booking.bundle_id).first() if booking.bundle_id else None
    checkin_notification = notify_check_in(
        booking_id=booking.booking_id,
        event_name=(_bundle.event_name if _bundle else None) or "Event",
        is_vendor=is_vendor,
        client_name=f"{client.f_name} {client.l_name}" if client else "Client",
        vendor_name=f"{vendor_user.f_name} {vendor_user.l_name}" if vendor_user else "Vendor",
        recipient_user=recipient_user,
        db=db,
    )
    logger.info("Check-in notification for booking %s: %s", booking.booking_id, checkin_notification)

    return {
        "message": (
            "Check-in successful. Funds have been released to you."
            if released else "Check-in successful."
        ),
        "distance_miles": round(distance, 2),
        "check_in_time": current_time,
        "funds_released": released,
        "notification": checkin_notification,
    }
