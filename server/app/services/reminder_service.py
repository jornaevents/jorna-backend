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


def _lead_phrase(*, when: str, now: datetime, start: Optional[datetime]) -> str:
    """How the email describes the timing.

    The automatic send is always half an hour out, so it could say so. A resend
    is whenever the client pressed the button, and "in about half an hour" sent
    at eight in the evening to a vendor whose set began at six is the kind of
    small lie that costs an app its credibility with the people it depends on.
    """
    if start is None:
        return f"starts at {when}"
    minutes = round((start - now).total_seconds() / 60)
    if minutes > 90:
        return f"starts at {when}"
    if minutes > 1:
        return f"starts at {when} — about {minutes} minutes from now"
    if minutes >= -5:
        return f"starts at {when} — any moment now"
    return f"started at {when}"


def _subject(*, where: str, now: datetime, start: Optional[datetime]) -> str:
    place = where or "check in when you arrive"
    if start is not None and now >= start:
        return f"Check in — {place}"
    return f"You're on shortly — {place}"


def _body(*, vendor_name: str, service_name: str, event_name: str, lead: str,
          where: str, url: str) -> tuple[str, str]:
    """The email, in both the shapes Resend wants.

    Short on purpose. It is read one-handed, in a car park, by somebody who is
    about to carry something heavy.
    """
    greeting = f"Hi {vendor_name}," if vendor_name else "Hi,"
    html = f"""
      <div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:480px;color:#35101b">
        <p>{greeting}</p>
        <p><strong>{service_name}</strong> for {event_name} {lead}.</p>
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
        f"{service_name} for {event_name} {lead}.\n"
        f"{where}\n\n"
        f"Check in when you arrive: {url}\n\n"
        "The link checks that you're at the venue, so open it once you're there. "
        "Checking in tells your client you've arrived and is your confirmation "
        "that the event went ahead — it's what releases your payment once they "
        "confirm too.\n"
    )
    return html, text


def _deliver(booking: Booking, db: Session, *, now: datetime) -> bool:
    """Build and send the email. Records nothing — the callers own their markers."""
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    user = (
        db.query(User).filter(User.user_id == vendor.user_id).first() if vendor else None
    )
    if not user or not user.email:
        logger.info("No email for the vendor on booking %s", booking.booking_id)
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
        lead=_lead_phrase(when=_local_time(booking, start), now=now, start=start),
        where=booking.location or "",
        url=checkin_url(booking, now=now),
    )

    result = send_email(
        to=user.email,
        subject=_subject(where=booking.location or "", now=now, start=start),
        html=html,
        text=text,
    )
    if not result.get("success"):
        logger.warning(
            "Check-in email for booking %s not sent: %s",
            booking.booking_id, result.get("error"),
        )
    return bool(result.get("success"))


def send_checkin_reminder(booking: Booking, db: Session, *, now: Optional[datetime] = None) -> bool:
    """The scheduled email. Returns whether it went.

    Marks the booking either way. A send that failed is not retried on the next
    sweep by design — the sweep runs every few minutes, and a retry loop against
    a bad address would mean a vendor's inbox filling up if it ever started
    working. The window is half an hour wide; a reminder that missed it has
    missed it, and the client can send it again by hand.
    """
    now = now or datetime.now(timezone.utc)
    sent = _deliver(booking, db, now=now)
    booking.checkin_reminder_sent_at = now
    db.commit()
    return sent


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


# ── The client's own nudge ────────────────────────────────────────────
#
# A vendor is on site, unloading, and hasn't checked in. The client can see that
# — their name has no "Here" against it on the day — and the useful thing to be
# able to do about it is send the email again. Per booking, because that's what
# a check-in is: this vendor, this service, this arrival.

# Long enough that pressing it twice in frustration doesn't send twice, short
# enough to be useful when a vendor says the first one never arrived.
RESEND_COOLDOWN = timedelta(minutes=10)

# When the button is worth offering at all. Wide enough to cover a vendor
# arriving early and one still packing down; not so wide that a client can nudge
# somebody about a wedding a fortnight away.
RESEND_OPENS = timedelta(hours=6)
RESEND_CLOSES = timedelta(hours=12)


def last_reminder_at(booking: Booking) -> Optional[datetime]:
    """The most recent email of either kind, scheduled or asked for.

    Both count towards the cooldown: a resend pressed a minute after the
    automatic one is the same email arriving twice.
    """
    stamps = [
        s for s in (booking.checkin_reminder_sent_at, booking.checkin_reminder_resent_at)
        if s is not None
    ]
    if not stamps:
        return None
    latest = max(stamps)
    # Stored naive (the column has no timezone) but always written as UTC.
    return latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)


def resend_state_from(
    booking: Booking,
    *,
    start: Optional[datetime],
    has_anchor: bool,
    now: datetime,
) -> dict:
    """The rule itself, with every lookup already done.

    Split out because a bundle's booking summaries are built without a session
    on purpose — so a list of bundles can batch its joins instead of issuing
    three queries per booking. Every booking in a bundle shares one check-in
    anchor, so the caller resolves it once and passes the answer in.
    """
    if booking.status not in COMMITTED_STATUSES:
        return {"can_resend": False, "reason": "This vendor hasn't taken the booking yet."}
    if booking.vendor_checked_in_at:
        return {"can_resend": False, "reason": "They've already checked in."}

    if start is None:
        return {"can_resend": False, "reason": "This booking has no date and time yet."}

    if not has_anchor:
        return {
            "can_resend": False,
            "reason": "There's nowhere to check in against until the plan has an address.",
        }

    if now < start - RESEND_OPENS:
        return {"can_resend": False, "reason": "Available closer to the day."}
    if now > start + RESEND_CLOSES:
        return {"can_resend": False, "reason": "This booking's day has passed."}

    last = last_reminder_at(booking)
    if last is not None and now - last < RESEND_COOLDOWN:
        return {
            "can_resend": False,
            "reason": "Just sent — you can send another in a few minutes.",
            "retry_at": (last + RESEND_COOLDOWN).isoformat(),
        }

    return {"can_resend": True, "reason": None}


def resend_state(booking: Booking, db: Session, *, now: Optional[datetime] = None) -> dict:
    """Whether the client may send this vendor their check-in email, and why not.

    The one definition, read both by the endpoint that does it and by the
    booking payload the client draws its button from — so the button is never
    offered for a call that must fail, the same contract checkin_anchor keeps.
    """
    lat, lng = checkin_anchor(booking, db)
    return resend_state_from(
        booking,
        start=starts_at(booking, db),
        has_anchor=lat is not None and lng is not None,
        now=now or datetime.now(timezone.utc),
    )


def resend_checkin_reminder(
    *, booking_id: str, user_id: str, db: Session, now: Optional[datetime] = None,
) -> dict:
    """Send this vendor their check-in email again, at the client's request.

    The client's, specifically: it's their event, and the vendor is the one
    person who shouldn't be able to make their own reminder arrive.
    """
    now = now or datetime.now(timezone.utc)

    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise ReminderError(404, "Booking not found")
    if booking.user_id != user_id:
        raise ReminderError(403, "This isn't your booking")

    state = resend_state(booking, db, now=now)
    if not state["can_resend"]:
        raise ReminderError(400, state["reason"])

    sent = _deliver(booking, db, now=now)
    # Recorded even when the send failed, so a client hammering a button against
    # a bounced address doesn't queue up a hundred of them. The message below
    # says what happened either way.
    booking.checkin_reminder_resent_at = now
    db.commit()

    return {
        "sent": sent,
        "message": (
            "Check-in email sent."
            if sent
            else "We couldn't reach that vendor by email. Try messaging them."
        ),
        "resent_at": now.isoformat(),
    }
