"""Business logic for bookings: create, status update, fetch, check-in."""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Booking, Bundle, Event, User, Vendor, Service
from app.models.schemas import BookingStatus
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


def vendor_has_conflicting_booking(
    *,
    vendor_id: str,
    date_iso: str | None,
    date_end: str | None,
    db: Session,
    exclude_booking_id: str | None = None,
) -> Booking | None:
    """Return an existing locked (approved/paid) booking for *vendor_id* whose
    date range overlaps ``[date_iso, date_end]``, or ``None`` if the vendor is
    free for that span.

    A vendor serves one event per day, so two locked bookings may not overlap.
    Single-day bookings store a null ``date_end`` — treated as ending on
    ``date_iso``. Overlap holds when the existing booking starts on or before the
    requested end AND ends on or after the requested start. A TBD/missing date
    can't conflict (nothing to compare against).
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
    return query.first()


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
    event_name: str = "Event",
) -> dict:
    """Send push notifications for a booking status change."""
    result = notify_booking_status_change(
        status=status,
        booking_id=booking.booking_id,
        event_name=event_name,
        service_name=service.name if service else "Service",
        client_name=f"{client.f_name} {client.l_name}" if client else "Client",
        vendor_name=f"{vendor_user.f_name} {vendor_user.l_name}" if vendor_user else "Vendor",
        client_fcm_token=client.fcm_token if client else None,
        vendor_fcm_token=vendor_user.fcm_token if vendor_user else None,
        client_email=client.email if client else None,
        vendor_email=vendor_user.email if vendor_user else None,
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
        "status": booking.status,
        "payment_status": booking.payment_status,
        "amount_cents": booking.amount_cents,
        "currency": booking.currency,
        "client_checked_in_at": booking.client_checked_in_at,
        "vendor_checked_in_at": booking.vendor_checked_in_at,
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
    }


# ── Service functions ─────────────────────────────────────────────────

# A venue booking stops anchoring the event once it's in one of these states.
_DEAD_BOOKING_STATUSES = ("rejected", "cancelled")
_DEAD_VENUE_PAYMENT_STATUSES = ("refunded",)


def _live_venue_booking(bundle_id: str | None, db: Session) -> tuple[Booking, Service] | None:
    """The bundle's live venue booking + its service, or None.

    "Live" = a venue-category service with GPS coords, whose booking hasn't been
    rejected/cancelled or refunded. This is the source of truth for whether the
    event currently has a venue — computed from the bookings themselves, so it's
    always correct without relying on a denormalized cache.
    """
    if not bundle_id:
        return None
    bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
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
            and (b.payment_status or "unpaid") not in _DEAD_VENUE_PAYMENT_STATUSES
        ):
            return b, s
    return None


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
        # Mirror onto the traveling vendors' bookings; the venue's own booking
        # already carries its service coords, so leave it untouched.
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
    return None


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
    date_iso: str | None = None,
    date_end: str | None = None,
    time_start: str | None = None,
    time_end: str | None = None,
) -> int | None:
    """Estimate a booking's total in cents = rate x quantity, per the service's
    price_unit. Returns None when the quantity can't be determined — the caller
    then leaves amount_cents nil and the booking prices at the flat rate.

    person -> guest_count; day -> days in the date range; hour -> hours in the
    event time window; event/unknown/missing-data -> None (flat rate).
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
        venue_latitude=venue_latitude,
        venue_longitude=venue_longitude,
        status=BookingStatus.PENDING.value,
        bundle_id=bundle_id,
    )
    # Estimate the total = rate x quantity from everything the booking carries.
    # When the quantity is still unknown (e.g. a per-person service with no guest
    # count), amount_cents stays nil and the price is resolved/guarded later —
    # never billed as the bare per-unit rate (see resolve_total_cents + checkout).
    est = estimate_amount_cents(
        service,
        guest_count=guest_count,
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

    client, vendor_obj, vendor_user, _ = _get_booking_parties(db, booking)
    try:
        notification = _dispatch_status_notification(
            BookingStatus.PENDING.value, booking, client, vendor_user, service,
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
        if booking.status not in _negotiable:
            raise BookingError(
                400, f"Cannot change status from {booking.status} to {status_str}"
            )

    # A vendor can't approve two bookings for overlapping dates — one event per
    # day. Checked only on approval (a pending request is just a lead); checkout
    # re-checks to catch the race between approval and payment.
    if status_str == BookingStatus.APPROVED.value:
        conflict = vendor_has_conflicting_booking(
            vendor_id=booking.vendor_id,
            date_iso=booking.date_iso,
            date_end=booking.date_end,
            db=db,
            exclude_booking_id=booking.booking_id,
        )
        if conflict:
            raise BookingError(
                409,
                "You already have a confirmed booking on that date. Reject or "
                "move it before approving another booking for the same day.",
            )

    booking.status = status_str
    if status_str == BookingStatus.APPROVED.value:
        booking.confirmed_at = datetime.now(timezone.utc)
    # A rejected venue no longer anchors the event — refresh so its cached coords
    # clear (check-in re-derives regardless, but keep the denormalized copies honest).
    if status_str == BookingStatus.REJECTED.value:
        db.flush()
        sync_event_venue(booking.bundle_id, db)
    db.commit()
    db.refresh(booking)

    client, vendor_obj, vendor_user, service = _get_booking_parties(db, booking)
    _bundle = db.query(Bundle).filter(Bundle.bundle_id == booking.bundle_id).first() if booking.bundle_id else None
    _event_name = (_bundle.event_name if _bundle else None) or "Event"
    try:
        notification = _dispatch_status_notification(
            status_str, booking, client, vendor_user, service, event_name=_event_name,
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

    allowed_statuses = {BookingStatus.PENDING.value, BookingStatus.NEGOTIATION_ONGOING.value}
    if booking.status not in allowed_statuses:
        raise BookingError(
            400,
            f"Booking cannot be edited in '{booking.status}' status. "
            "Changes are only allowed while the booking is pending or under negotiation.",
        )

    allowed_fields = {"date_iso", "date_end", "guest_count", "time_start", "time_end", "location", "venue_latitude", "venue_longitude"}
    quantity_fields = {"date_iso", "date_end", "guest_count", "time_start", "time_end"}
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
    """Return a paginated list of bookings directed to a vendor."""
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise BookingError(404, "Vendor not found")
    if vendor.user_id != caller_user_id:
        raise BookingError(403, "You are not authorised to view these bookings")
    query = db.query(Booking).filter(Booking.vendor_id == vendor_id)
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": [_booking_dict(b, db) for b in items], "total": total, "limit": limit, "offset": offset}


def event_confirmable_date(booking: Booking) -> tuple[bool, str | None]:
    """Whether the event's date has arrived, so the booking may be confirmed for
    escrow release. Returns (ok, error_message).

    Gates on the booking's LAST day (date_end for a multi-day event, else date_iso):
    funds are held until the event has taken place, so neither party can confirm —
    and release — before it. A TBD / unparseable date is NOT confirmable: a real
    date must be set first, so an unscheduled event can never release escrow.
    """
    start = _parse_date(booking.date_iso)
    end = _parse_date(booking.date_end) or start
    if end is None:
        return False, (
            "This booking has no scheduled date yet. Set the event date before "
            "confirming — the payment is held until the event has taken place."
        )
    today = datetime.now(timezone.utc).date()
    if today < end:
        human = f"{end.strftime('%B')} {end.day}, {end.year}"
        return False, f"You can confirm once the event has taken place, on or after {human}."
    return True, None


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
    live_venue = _live_venue_booking(booking.bundle_id, db)
    if live_venue:
        venue_lat, venue_lng = live_venue[1].venue_latitude, live_venue[1].venue_longitude
    elif not booking.bundle_id:
        # Direct, non-bundled booking: its own pin is the anchor.
        venue_lat, venue_lng = booking.venue_latitude, booking.venue_longitude
    else:
        venue_lat = venue_lng = None

    if venue_lat is None or venue_lng is None:
        raise BookingError(400, "This event has no venue set yet — check-in becomes available once a venue is booked.")

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
        # The vendor's GPS check-in is their event-completion confirmation. Record
        # it whenever they're at the venue — including early, or on day 1 of a
        # multi-day booking — so it's never stranded waiting for a second check-in.
        # This can't release funds prematurely: release also needs the CUSTOMER's
        # confirmation, and that is gated on the event date (see confirm_event).
        if booking.payment_status == "paid" and not booking.vendor_confirmed_at:
            booking.vendor_confirmed_at = datetime.now(timezone.utc)
            if booking.customer_confirmed_at:
                # Both parties are now in — pay out the vendor. Best-effort: a
                # Stripe failure must not fail the check-in the vendor just made.
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

    # Notify the other party
    client, vendor_obj, vendor_user, _ = _get_booking_parties(db, booking)
    recipient_token = (
        client.fcm_token
        if (is_vendor and client)
        else (vendor_user.fcm_token if vendor_user else None)
    )
    _bundle = db.query(Bundle).filter(Bundle.bundle_id == booking.bundle_id).first() if booking.bundle_id else None
    checkin_notification = notify_check_in(
        booking_id=booking.booking_id,
        event_name=(_bundle.event_name if _bundle else None) or "Event",
        is_vendor=is_vendor,
        client_name=f"{client.f_name} {client.l_name}" if client else "Client",
        vendor_name=f"{vendor_user.f_name} {vendor_user.l_name}" if vendor_user else "Vendor",
        recipient_fcm_token=recipient_token,
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
