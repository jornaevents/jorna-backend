"""Is this booking fit to send to a vendor, and what may still be changed.

A vendor accepting a request is agreeing to be somewhere, at a time, for a
price. If any of those three is unknown the request isn't really a request —
it's a question the client hasn't answered yet, and answering it is not the
vendor's job.

The web has had this rule since the send button existed (lib/planning's
bookingGaps), but only as a disabled button: select_bundle accepted anything,
so iOS or a direct API call sent regardless. This is the same rule where it can
actually be enforced. The two are deliberately identical, field for field —
a client that greys out a button the server would have accepted is annoying; one
that offers a button the server refuses is worse.

The second half is the other side of the same idea. Once a request has gone out,
what a vendor was told is what they answered, so the details they were shown
stop being editable. A field that was *empty* can still be filled: that isn't
changing a commitment, it's completing one, and without it a booking that
reached a vendor short of a headcount would be unpayable for ever.
"""

import re
from datetime import date
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.db.models import Booking, Bundle, Event, Service

# The details a vendor is shown, and therefore the ones that stop being the
# client's to change once they've seen them. venue_latitude/longitude are
# deliberately absent — they're a cache of the same address, refreshed by
# sync_event_venue, and freezing them would break venue syncing without
# protecting anything a vendor reads.
COMMITTED_FIELDS = (
    "date_iso",
    "date_end",
    "guest_count",
    "performer_count",
    "time_start",
    "time_end",
    "location",
)

US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY", "PR",
}

_ZIP = re.compile(r"^\d{5}(-\d{4})?$")


def is_complete_address(raw: Optional[str]) -> bool:
    """A street, a city, a state and a ZIP — what a vendor needs to drive to it.

    Mirrors the web's parseAddress + addressGaps. A second line is optional;
    everything else is what makes the difference between an address and a
    gesture at one.
    """
    text = (raw or "").strip()
    if not text:
        return False

    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return False

    state = ""
    zip_code = ""

    # The tail may be "IL 60601", "IL", or "60601" — take whichever it is.
    last = parts[-1]
    state_zip = re.match(r"^([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)$", last)
    if state_zip and state_zip.group(1).upper() in US_STATE_CODES:
        state = state_zip.group(1).upper()
        zip_code = state_zip.group(2)
        parts.pop()
    elif _ZIP.match(last):
        zip_code = last
        parts.pop()
        if parts and parts[-1].upper() in US_STATE_CODES:
            state = parts.pop().upper()
    elif len(last) == 2 and last.upper() in US_STATE_CODES:
        state = last.upper()
        parts.pop()

    city = parts.pop() if parts else ""
    line1 = parts.pop(0) if parts else ""

    return bool(line1 and city and state in US_STATE_CODES and _ZIP.match(zip_code))


def _price_unit_kind(unit: Optional[str]) -> str:
    """What quantity this rate multiplies by.

    Delegates to the pricer's own normaliser rather than restating it. price_unit
    is free text a vendor types, and _normalize_unit accepts what they actually
    type — "per head", "guest", "plate", "pax", "persons" all mean per person,
    and "hourly" means per hour.

    This used to be its own narrow list matching only "person", "day" and
    "hour". A caterer priced "per head" therefore needed no guest count to send,
    while resolve_total_cents read the same field as per person and returned no
    total at all: the request went to the vendor, the vendor accepted, and the
    booking arrived at checkout unpayable. Two functions disagreeing about one
    string, which is the reason this one no longer holds an opinion.
    """
    from app.services.booking_service import _normalize_unit

    return _normalize_unit(unit) or "event"


def is_unset(value: Optional[str]) -> bool:
    """Whether a stored answer is really an answer.

    "TBD" is what the bundle builder writes when it wasn't told — a placeholder
    that is a non-empty string, so every plain truthiness check read it as a
    filled-in value. It means the same as blank and has to be treated the same.
    """
    text = (value or "").strip()
    return not text or text.upper() == "TBD"


def is_in_the_past(date_iso: Optional[str]) -> bool:
    """Whether this date has already happened, going out.

    A date that's merely set isn't the same question as a date that's still
    ahead of us — nothing here checked the second one, so a plan built on a
    mistyped year (or one nobody noticed had slipped by) sent a vendor a real
    request to hold a day that had already passed. Same-day still counts:
    only strictly before today is a gap, not today itself.
    """
    if is_unset(date_iso):
        return False
    try:
        return date.fromisoformat(date_iso) < date.today()
    except ValueError:
        # Malformed rather than merely blank — not this function's job to
        # flag, and the cautious direction is not to block on it.
        return False


def booking_gaps(
    booking: Booking,
    service: Optional[Service],
    *,
    event_location: Optional[str] = None,
) -> list[str]:
    """What this booking is still missing, in words a client can act on.

    A date, a place and a time on every booking, whatever it costs: a vendor
    accepting is agreeing to be somewhere at a time, and that is true of a
    flat-rate DJ exactly as it is of an hourly one.

    Times used to be asked for only when the *price* depended on them, which is
    a different question and answered it wrongly — a per-event or per-person
    booking sailed through the send gate carrying "TBD" for both, and vendors
    were asked to hold a day with no hours attached to it. The pricing units
    still decide which *quantity* is needed on top: per person wants a headcount,
    per day wants the dates it spans.
    """
    gaps: list[str] = []

    if is_unset(booking.date_iso):
        gaps.append("a date")
    elif is_in_the_past(booking.date_iso):
        gaps.append("a date that hasn't already passed")

    # The booking's own location, or the event's — a booking made from an event
    # inherits it, and a venue brings its own.
    where = booking.location or event_location or ""
    if not is_complete_address(where):
        gaps.append("a full address")

    if is_unset(booking.time_start) or is_unset(booking.time_end):
        gaps.append("a start and end time")

    kind = _price_unit_kind(service.price_unit if service else None)
    # A vendor can also opt into demanding either count regardless of price
    # unit (Service.require_guest_count / require_performer_count) — a flat
    # rate caterer who still wants a headcount before deciding, say. That's
    # additive on top of the price-unit-driven requirement below, never a way
    # to loosen it, and the two checks are independent: a service could in
    # principle need both at once.
    if kind == "person" or (service and service.require_guest_count):
        # The booking's own count, not the event's: the total is resolved from
        # the booking that's priced, so an event-level headcount would satisfy
        # this check without satisfying checkout.
        if not (booking.guest_count or 0):
            gaps.append("a guest count")
    if kind == "performer" or (service and service.require_performer_count):
        if not (booking.performer_count or 0):
            gaps.append("a performer count")

    return gaps


def describe_gaps(gaps: Iterable[str]) -> str:
    """"a date and a full address" — one phrase, for the end of a sentence."""
    items = list(gaps)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


# ── What a vendor has already been told ───────────────────────────────


def has_reached_vendor(booking: Booking, db: Session) -> bool:
    """Whether anybody outside this client has seen this booking yet.

    A draft is the client still thinking, and nothing in one has been sent:
    create_booking suppresses the vendor notification while the parent bundle is
    a draft, and select_bundle is what sends the lot. So the bundle's status is
    the honest answer to "has a vendor seen this", and it's the same signal the
    web reads to decide whether a plan is still a draft.
    """
    if not booking.bundle_id:
        # No bundle to be a draft. Bookings are always created with one, so this
        # is a legacy row — treat it as sent, which is the cautious direction.
        return True
    bundle = db.query(Bundle).filter(Bundle.bundle_id == booking.bundle_id).first()
    return bundle is not None and bundle.status != "draft"


def locked_fields_for(booking: Booking, *, reached: bool) -> list[str]:
    """The rule itself, with the one lookup already done.

    Split out so a screen serializing many bookings can answer "has a vendor
    seen this" once per plan instead of once per booking — the bundle's status
    is a property of the bundle, not of any row in it.
    """
    if not reached:
        return []
    return [f for f in COMMITTED_FIELDS if _has_value(getattr(booking, f, None))]


def locked_fields(booking: Booking, db: Session) -> list[str]:
    """The details this client may no longer change.

    Empty while a plan is a draft. Once it's out, every field that carries a
    value — because that value is what a vendor was asked about. A field that is
    still blank isn't locked: filling it completes the request rather than
    altering it, and a booking that went out short of a headcount has to be able
    to gain one or it can never be paid.
    """
    return locked_fields_for(booking, reached=has_reached_vendor(booking, db))


def _has_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        # "TBD" is how an unset date is stored, and it is not an answer.
        return bool(value.strip()) and value.strip() != "TBD"
    return True


def refuse_locked_changes(booking: Booking, update_data: dict, db: Session) -> None:
    """Raise if an update would change something a vendor has already been told.

    Only a real change counts: sending back the value that's already there is
    what a form does when it saves every field it holds, and refusing that would
    make the client's own autosave impossible.
    """
    locked = set(locked_fields(booking, db))
    if not locked:
        return

    changing = [
        field
        for field, value in update_data.items()
        if field in locked and value != getattr(booking, field, None)
    ]
    if not changing:
        return

    from app.services.booking_service import BookingError

    raise BookingError(
        400,
        "This request is already with the vendor, so its "
        f"{describe_gaps(_field_names(changing))} can't be changed. "
        "Cancel it and book again if the plan has moved.",
    )


_FIELD_NAMES = {
    "date_iso": "date",
    "date_end": "end date",
    "guest_count": "guest count",
    "performer_count": "performer count",
    "time_start": "start time",
    "time_end": "end time",
    "location": "address",
}


def _field_names(fields: Iterable[str]) -> list[str]:
    return [_FIELD_NAMES.get(f, f) for f in fields]


# ── A whole plan ──────────────────────────────────────────────────────


def plan_gaps(
    bundle_id: str, db: Session, *, only_statuses: Optional[tuple[str, ...]] = None,
) -> list[tuple[Booking, list[str]]]:
    """Every live booking in this plan that isn't fit to send, and why.

    ``only_statuses`` narrows it to the bookings a particular send would
    actually notify — re-sending a plan asks the vendors who haven't answered,
    and holding that up over a booking somebody already accepted would be
    refusing to do something harmless.
    """
    from app.services.booking_service import _DEAD_BOOKING_STATUSES

    bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    if not bookings:
        return []

    service_ids = [b.service_id for b in bookings if b.service_id]
    services = (
        {s.service_id: s for s in db.query(Service).filter(Service.service_id.in_(service_ids)).all()}
        if service_ids
        else {}
    )

    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    event = (
        db.query(Event).filter(Event.event_id == bundle.event_id).first()
        if bundle and bundle.event_id
        else None
    )
    event_location = event.location if event else None

    out = []
    for b in bookings:
        if b.status in _DEAD_BOOKING_STATUSES:
            continue
        if only_statuses is not None and b.status not in only_statuses:
            continue
        gaps = booking_gaps(b, services.get(b.service_id), event_location=event_location)
        if gaps:
            out.append((b, gaps))
    return out
