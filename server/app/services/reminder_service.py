"""The half-hour warning, and the check-in link inside it.

A vendor arrives at a hall, sets up, and works. Opening an app to say they're
there is the kind of task that gets remembered at eleven at night, and their
check-in is also their half of releasing escrow — so a forgotten one is a
vendor's own money sitting still. The email arrives while they're parking.

Timed per booking rather than per celebration. Each booking carries its own
start, and they differ on purpose: the mehndi artist is at the house at ten
while the caterer is at the hall at four. One email at "the event start" would
be wrong for nearly everyone it went to.

Nothing here decides anything about money. It sends a link. The link still has
to prove where the phone holding it is standing, exactly as the in-app button
does — see check_in_by_token.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from sqlalchemy.orm import Session

from app.config import ALGORITHM, SECRET_KEY, WEB_APP_URL
from app.db.models import Booking, Bundle, Service, User, Vendor
from app.services.booking_service import checkin_anchor
from app.services.email_service import send_email
from app.services.timezone_service import local_to_utc

logger = logging.getLogger(__name__)

# How long before the start the email goes out.
REMINDER_LEAD = timedelta(minutes=30)

# How late a start may already be and still earn a reminder. The sweep can be
# held up — a deploy, a restart, a slow pass — and a booking whose moment came
# and went during the gap should still be told. Bounded so a sweep resuming
# after a long outage doesn't post yesterday's reminders.
REMINDER_GRACE = timedelta(minutes=20)

# Statuses where a vendor has actually agreed to be there. A pending request is
# a question nobody has answered; nothing should tell that vendor to turn up.
COMMITTED_STATUSES = ("approved", "payment_confirmed")

# The link is live from the reminder until well after the night ends, so a
# vendor who checks in during the packing-down isn't turned away by an expiry.
TOKEN_LIFETIME = timedelta(hours=18)


class ReminderError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ── The link ──────────────────────────────────────────────────────────


def checkin_token(booking: Booking, *, now: Optional[datetime] = None) -> str:
    """A signed link for one booking's check-in.

    Signed rather than stored: it expires on its own, it can't be enumerated,
    and a booking that never gets one costs nothing. The claim is narrow — this
    booking, this purpose — so it can't be replayed against any other endpoint
    that reads our tokens.
    """
    now = now or datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": booking.booking_id,
            # Checked on the way in. Without it a token minted here would be
            # accepted anywhere get_current_user is, as whoever "sub" names.
            "purpose": "checkin",
            "exp": now + TOKEN_LIFETIME,
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def booking_for_token(token: str, db: Session) -> Booking:
    """The booking a check-in link refers to, or a reason it doesn't."""
    try:
        claims = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise ReminderError(401, "This check-in link has expired. Open the app to check in.")
    except jwt.InvalidTokenError:
        raise ReminderError(401, "This check-in link isn't valid.")

    if claims.get("purpose") != "checkin":
        raise ReminderError(401, "This check-in link isn't valid.")

    booking = db.query(Booking).filter(Booking.booking_id == claims.get("sub")).first()
    if not booking:
        raise ReminderError(404, "That booking no longer exists.")
    return booking


def checkin_url(booking: Booking, *, now: Optional[datetime] = None) -> str:
    return f"{WEB_APP_URL}/check-in/?t={checkin_token(booking, now=now)}"


# ── Who is due ────────────────────────────────────────────────────────


def starts_at(booking: Booking, db: Session) -> Optional[datetime]:
    """The real moment this booking starts, or None if that can't be worked out.

    The venue's clock, not the server's. A stored "18:00" is a wall-clock time
    with no timezone on it, and reading it as UTC would put a Chicago reception
    six hours out — see timezone_service.
    """
    return local_to_utc(
        date_iso=booking.date_iso,
        time_hhmm=booking.time_start,
        location=booking.location,
        latitude=booking.venue_latitude,
        longitude=booking.venue_longitude,
    )


def due_for_reminder(*, now: datetime, db: Session) -> list[Booking]:
    """Bookings whose vendor should be told now.

    Deliberately re-derives the start rather than filtering it in SQL: the start
    isn't a column, it's a date string and a time string read through a timezone
    that depends on the address. So the query narrows on what it can — the right
    day, committed, not already reminded, not already checked in — and the
    moment is worked out per row.
    """
    # A day either side, because the venue's clock can be some hours from the
    # server's and a local date can span two UTC ones.
    days = {
        (now + timedelta(days=offset)).strftime("%Y-%m-%d") for offset in (-1, 0, 1)
    }

    candidates = (
        db.query(Booking)
        .filter(
            Booking.date_iso.in_(days),
            Booking.status.in_(COMMITTED_STATUSES),
            Booking.checkin_reminder_sent_at.is_(None),
            # Somebody already there doesn't need telling to go.
            Booking.vendor_checked_in_at.is_(None),
        )
        .all()
    )

    due = []
    for booking in candidates:
        start = starts_at(booking, db)
        if start is None:
            continue
        # The window: from half an hour before the start until a short way past
        # it. Open at both ends so a sweep that runs late still catches it.
        if not (start - REMINDER_LEAD <= now <= start + REMINDER_GRACE):
            continue
        # The whole email is a check-in button, and check-in is measured from
        # the venue anchor. No anchor, no check-in — so sending would be
        # promising a link that refuses. checkin_anchor is the one definition of
        # that, the same one the in-app button is offered on.
        lat, lng = checkin_anchor(booking, db)
        if lat is None or lng is None:
            logger.info(
                "Booking %s is due a reminder but has nowhere to check in against",
                booking.booking_id,
            )
            continue
        due.append(booking)
    return due


# ── The email ─────────────────────────────────────────────────────────


def _local_time(booking: Booking, start: Optional[datetime]) -> str:
    """"6:00 PM" in the venue's own time, which is the only one a vendor cares
    about. Falls back to what's stored when the zone couldn't be resolved."""
    if start is None:
        return booking.time_start or ""
    from app.services.timezone_service import zone_for

    zone = zone_for(
        location=booking.location,
        latitude=booking.venue_latitude,
        longitude=booking.venue_longitude,
    )
    local = start.astimezone(zone) if zone else start
    # Built by hand rather than with strftime: "%-I" is a glibc extension that
    # raises on Windows, and the tests run there.
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def _body(*, vendor_name: str, service_name: str, event_name: str, when: str,
          where: str, url: str) -> tuple[str, str]:
    """The email, in both the shapes Resend wants.

    Short on purpose. It is read one-handed, in a car park, by somebody who is
    about to carry something heavy.
    """
    greeting = f"Hi {vendor_name}," if vendor_name else "Hi,"
    html = f"""
      <div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:480px;color:#35101b">
        <p>{greeting}</p>
        <p><strong>{service_name}</strong> for {event_name} starts at {when}, in about half an hour.</p>
        <p style="color:#6b5c50">{where}</p>
        <p style="margin:28px 0">
          <a href="{url}"
             style="background:#6b1226;color:#f6eee1;text-decoration:none;padding:14px 28px;border-radius:999px;font-weight:600;display:inline-block">
            Check in when you arrive
          </a>
        </p>
        <p style="color:#6b5c50;font-size:14px">
          The link checks that you're at the venue, so open it once you're there.
          Checking in tells your client you've arrived and is your confirmation
          that the event went ahead — it's what releases your payment once they
          confirm too.
        </p>
      </div>
    """.strip()

    text = (
        f"{greeting}\n\n"
        f"{service_name} for {event_name} starts at {when}, in about half an hour.\n"
        f"{where}\n\n"
        f"Check in when you arrive: {url}\n\n"
        "The link checks that you're at the venue, so open it once you're there. "
        "Checking in tells your client you've arrived and is your confirmation "
        "that the event went ahead — it's what releases your payment once they "
        "confirm too.\n"
    )
    return html, text


def send_checkin_reminder(booking: Booking, db: Session, *, now: Optional[datetime] = None) -> bool:
    """Email one vendor. Returns whether it went.

    Marks the booking either way. A send that failed is not retried on the next
    sweep by design — the sweep runs every few minutes, and a retry loop against
    a bad address would mean a vendor's inbox filling up if it ever started
    working. The window is half an hour wide; a reminder that missed it has
    missed it, and the app still has the button.
    """
    now = now or datetime.now(timezone.utc)

    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    user = (
        db.query(User).filter(User.user_id == vendor.user_id).first() if vendor else None
    )
    if not user or not user.email:
        logger.info("No email for the vendor on booking %s", booking.booking_id)
        booking.checkin_reminder_sent_at = now
        db.commit()
        return False

    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    bundle = (
        db.query(Bundle).filter(Bundle.bundle_id == booking.bundle_id).first()
        if booking.bundle_id
        else None
    )

    start = starts_at(booking, db)
    html, text = _body(
        vendor_name=(user.f_name or "").strip(),
        service_name=(service.name if service else "Your booking"),
        event_name=(bundle.event_name if bundle and bundle.event_name else "a celebration"),
        when=_local_time(booking, start),
        where=booking.location or "",
        url=checkin_url(booking, now=now),
    )

    result = send_email(
        to=user.email,
        subject=f"You're on in 30 minutes — {booking.location or 'check in when you arrive'}",
        html=html,
        text=text,
    )

    booking.checkin_reminder_sent_at = now
    db.commit()

    if not result.get("success"):
        logger.warning(
            "Check-in reminder for booking %s not sent: %s",
            booking.booking_id, result.get("error"),
        )
    return bool(result.get("success"))


def send_due_reminders(*, db: Session, now: Optional[datetime] = None) -> int:
    """One sweep. Returns how many emails went out."""
    now = now or datetime.now(timezone.utc)
    sent = 0
    for booking in due_for_reminder(now=now, db=db):
        try:
            if send_checkin_reminder(booking, db, now=now):
                sent += 1
        except Exception as exc:  # one bad booking must not stop the rest
            logger.warning("Reminder for booking %s failed: %s", booking.booking_id, exc)
            db.rollback()
    if sent:
        logger.info("Sent %d check-in reminders", sent)
    return sent
