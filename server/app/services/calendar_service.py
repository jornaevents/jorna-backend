"""Business logic for Google Calendar OAuth and vendor availability."""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

# google_auth_oauthlib raises an error if Google returns extra scopes (e.g. openid,
# userinfo.email) beyond what was explicitly requested. Relaxing this is safe here
# because we only use the calendar scope — the extra ones come from Google automatically.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from googleapiclient.errors import HttpError
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import GOOGLE_CALENDAR_WEBHOOK_URL, SECRET_KEY
from app.db.models import Bundle, Service, Vendor, VendorAvailability, Booking
from app.utils.calendar import (
    get_google_auth_flow,
    create_google_calendar_service,
    get_freebusy_schedule,
)

# How far ahead the busy-block cache reaches. Generous on purpose — the
# overwhelming majority of browsing and booking happens well inside six
# months out; a request that reaches past it falls back to a live freebusy
# call for just that request rather than trying to cache every possible
# future date range.
BUSY_CACHE_DAYS_AHEAD = 180

# Google's own cap on how long an events.watch() channel may run before it
# must be re-created — the renewal sweep re-watches anything closer to this
# than its own interval, so a channel is never allowed to actually lapse.
CHANNEL_LIFETIME = timedelta(days=7)

logger = logging.getLogger(__name__)


class CalendarError(Exception):
    """Raised when a calendar operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ── PKCE helpers ──────────────────────────────────────────────────────


def _generate_code_verifier() -> str:
    """Return a high-entropy random string for use as a PKCE code_verifier."""
    return secrets.token_urlsafe(96)


def _sign(payload: str) -> str:
    """Return a hex HMAC-SHA256 signature of payload using SECRET_KEY."""
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _encode_state(vendor_id: str, code_verifier: str, client: str = "ios") -> str:
    """Pack vendor_id + code_verifier + client into a signed state param.

    Format: <base64-payload>.<hmac-signature>
    The signature prevents forged state params (CSRF protection).

    ``client`` rides along because the callback is the only place that knows
    where to send the browser afterwards, and by then the request that started
    the flow is long gone — Google is the caller. Signed with the rest, so it
    can't be swapped in transit to redirect the vendor somewhere else.
    """
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {"vendor_id": vendor_id, "code_verifier": code_verifier, "client": client}
        ).encode()
    ).decode()
    return f"{payload}.{_sign(payload)}"


def decode_state(state: str) -> tuple[str, str, str]:
    """Verify the HMAC signature and decode state into (vendor_id, code_verifier, client).

    Raises CalendarError 400 if the state is malformed or the signature is invalid.
    A state without a client is one issued before web support existed, or by an
    older app — those are iOS, which is where the flow used to end.
    """
    try:
        payload, sig = state.rsplit(".", 1)
    except ValueError:
        raise CalendarError(400, "Invalid OAuth state parameter")

    if not hmac.compare_digest(sig, _sign(payload)):
        raise CalendarError(400, "Invalid OAuth state signature")

    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
        return data["vendor_id"], data["code_verifier"], data.get("client", "ios")
    except Exception:
        raise CalendarError(400, "Invalid OAuth state parameter")


# ── OAuth flow ────────────────────────────────────────────────────────


def get_google_auth_url(*, vendor_id: str, redirect_uri: str, client: str = "ios") -> dict:
    """Build a Google OAuth authorization URL for the vendor.

    Uses PKCE (code_verifier / code_challenge) to satisfy Google's requirement.
    The code_verifier is encoded into the state parameter so it survives the
    round-trip to Google and back to the callback endpoint.
    """
    try:
        flow = get_google_auth_flow(redirect_uri)
    except FileNotFoundError as e:
        raise CalendarError(500, str(e))

    code_verifier = _generate_code_verifier()

    # Setting flow.code_verifier causes google_auth_oauthlib to automatically
    # include code_challenge + code_challenge_method=S256 in the auth URL.
    flow.code_verifier = code_verifier

    state = _encode_state(vendor_id, code_verifier, client)

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        state=state,
        prompt="consent",
    )
    return {"auth_url": auth_url}


def handle_google_callback(
    *,
    vendor_id: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    db: Session,
) -> dict:
    """Exchange the auth code for tokens and persist them on the vendor row."""
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise CalendarError(404, "Vendor not found")

    try:
        flow = get_google_auth_flow(redirect_uri)
        # Pass code_verifier explicitly — more reliable than flow.code_verifier
        # across different versions of google_auth_oauthlib.
        flow.fetch_token(code=code, code_verifier=code_verifier)
        credentials = flow.credentials

        vendor.google_access_token = credentials.token
        vendor.google_refresh_token = credentials.refresh_token
        vendor.calendar_id = "primary"
        # What Google actually granted, not what SCOPES asked for — a vendor
        # can decline part of the consent screen, and Google never widens a
        # standing grant on its own later. This is the one place a token is
        # minted, so it's the one place that can know.
        vendor.google_granted_scopes = " ".join(credentials.scopes or [])

        db.commit()
    except Exception as e:
        raise CalendarError(400, f"Failed to fetch Google tokens: {str(e)}")

    # Warm the busy-block cache immediately rather than leaving it empty
    # until the next sweep, and open a push-notification channel so future
    # changes refresh it without waiting on one. Both best-effort — a vendor
    # is connected either way; these just decide how fresh the display is
    # in the meantime.
    refresh_busy_cache(vendor, db)
    watch_calendar(vendor, db)

    return {"message": "Google Calendar successfully connected"}


# ── Scope check ───────────────────────────────────────────────────────

WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def has_calendar_write_access(vendor: Vendor) -> bool:
    """Whether this vendor's own Google grant covers writing events —
    checked, never assumed, since SCOPES describes what's requested on a
    fresh connect, not what any particular vendor's token actually carries.
    """
    return bool(vendor.google_access_token) and WRITE_SCOPE in (
        vendor.google_granted_scopes or ""
    )


# ── Write-back ────────────────────────────────────────────────────────
#
# Jorna → the vendor's own Google Calendar, one event per approved booking.
# Both functions are best-effort by the same contract as every other
# best-effort write in this codebase (post_offer_message, notify_*): a
# calendar write failing must never fail the booking action that triggered
# it, so every call site wraps the whole body and only logs.


def _booking_datetimes(booking: Booking) -> tuple[datetime, datetime] | None:
    """Same construction get_vendor_availability uses for internal busy
    blocks (booking_service.get_vendor_availability) — treats the stored
    wall-clock hours as UTC. None when the hours aren't real yet ("TBD" or
    unset): a calendar event needs actual times, and guessing an all-day
    span here would tell the vendor something the booking hasn't said.
    """
    last_day = booking.date_end or booking.date_iso
    try:
        start = datetime.fromisoformat(f"{booking.date_iso}T{booking.time_start}:00+00:00")
        end = datetime.fromisoformat(f"{last_day}T{booking.time_end}:00+00:00")
        if end <= start:
            end += timedelta(days=1)
        return start, end
    except (ValueError, TypeError):
        return None


def sync_booking_to_calendar(booking: Booking, db: Session) -> None:
    """Create or update this booking as an event on the vendor's own Google
    Calendar. No-ops silently if the vendor isn't connected, connected
    read-only, or the booking has no real hours yet — none of those are
    errors, just nothing to do.
    """
    try:
        vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
        if not vendor or not has_calendar_write_access(vendor):
            return

        when = _booking_datetimes(booking)
        if when is None:
            return
        start, end = when

        service = db.query(Service).filter(Service.service_id == booking.service_id).first()
        bundle = (
            db.query(Bundle).filter(Bundle.bundle_id == booking.bundle_id).first()
            if booking.bundle_id
            else None
        )
        event_name = (bundle.event_name if bundle else None) or "Jorna booking"
        summary = f"{event_name} — {service.name}" if service else event_name

        body = {
            "summary": summary,
            "description": "Booked through Jorna.",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        if booking.location:
            body["location"] = booking.location

        gcal, creds = create_google_calendar_service(
            vendor.google_access_token, vendor.google_refresh_token
        )
        calendar_id = vendor.calendar_id or "primary"

        if booking.google_event_id:
            created = gcal.events().update(
                calendarId=calendar_id, eventId=booking.google_event_id, body=body
            ).execute()
        else:
            created = gcal.events().insert(calendarId=calendar_id, body=body).execute()
            booking.google_event_id = created.get("id")

        if creds.token and creds.token != vendor.google_access_token:
            vendor.google_access_token = creds.token

        db.commit()
    except Exception as exc:
        logger.warning(
            "Couldn't sync booking %s to Google Calendar: %s", booking.booking_id, exc
        )


def remove_booking_from_calendar(booking: Booking, db: Session) -> None:
    """Delete this booking's event, if write-back ever created one. Safe to
    call on a booking that never had one (no-op) or whose vendor has since
    disconnected (just clears the id — nothing to delete against)."""
    if not booking.google_event_id:
        return
    try:
        vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
        if not vendor or not vendor.google_access_token:
            booking.google_event_id = None
            db.commit()
            return

        gcal, creds = create_google_calendar_service(
            vendor.google_access_token, vendor.google_refresh_token
        )
        calendar_id = vendor.calendar_id or "primary"
        try:
            gcal.events().delete(
                calendarId=calendar_id, eventId=booking.google_event_id
            ).execute()
        except HttpError as exc:
            # Already gone — deleted by hand on the vendor's side, say — is
            # the outcome this call wanted anyway, not a failure of it.
            if exc.resp.status != 404:
                raise

        if creds.token and creds.token != vendor.google_access_token:
            vendor.google_access_token = creds.token
        booking.google_event_id = None
        db.commit()
    except Exception as exc:
        logger.warning(
            "Couldn't remove booking %s from Google Calendar: %s", booking.booking_id, exc
        )


# ── Busy-block cache + push notifications ────────────────────────────
#
# get_vendor_availability used to call Google's freebusy API live on every
# request. This section keeps a per-vendor cache warm instead, refreshed by
# whichever of three triggers fires first: a Google push notification (near
# real-time), the periodic re-sync sweep (a safety net for a missed or
# lapsed notification), or a request landing past the cached window (a
# direct live fallback, scoped to just that request).


def refresh_busy_cache(vendor: Vendor, db: Session, *, now: datetime | None = None) -> bool:
    """Re-fetch this vendor's Google busy blocks for the cached window and
    store them. Best-effort — returns whether it actually refreshed.
    """
    if not vendor.google_access_token:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        service, creds = create_google_calendar_service(
            vendor.google_access_token, vendor.google_refresh_token
        )
        window_end = now + timedelta(days=BUSY_CACHE_DAYS_AHEAD)
        busy = get_freebusy_schedule(
            service,
            now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        vendor.google_busy_cache = [
            {"start": b["start"], "end": b["end"]} for b in busy
        ]
        vendor.google_busy_synced_at = now

        if creds.token and creds.token != vendor.google_access_token:
            vendor.google_access_token = creds.token

        db.commit()
        return True
    except Exception as exc:
        logger.warning(
            "Couldn't refresh Google busy cache for vendor %s: %s", vendor.vendor_id, exc
        )
        return False


def watch_calendar(vendor: Vendor, db: Session, *, now: datetime | None = None) -> bool:
    """Ask Google to POST GOOGLE_CALENDAR_WEBHOOK_URL when this vendor's
    calendar changes. google_channel_id is generated fresh each call — high
    entropy and never exposed anywhere else, so it doubles as the channel's
    bearer credential: the webhook handler trusts a POST that quotes it back
    correctly, the same way a bearer token is trusted. Best-effort — a
    vendor stays connected and cached either way; this only affects how
    quickly a change is noticed.
    """
    if not vendor.google_access_token:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        service, creds = create_google_calendar_service(
            vendor.google_access_token, vendor.google_refresh_token
        )
        channel_id = secrets.token_urlsafe(32)
        calendar_id = vendor.calendar_id or "primary"
        resp = service.events().watch(
            calendarId=calendar_id,
            body={
                "id": channel_id,
                "type": "web_hook",
                "address": GOOGLE_CALENDAR_WEBHOOK_URL,
            },
        ).execute()

        vendor.google_channel_id = channel_id
        vendor.google_channel_resource_id = resp.get("resourceId")
        # Google returns expiration as a string of milliseconds since epoch;
        # fall back to our own cap if it's missing rather than leave the
        # channel with no renewal date at all.
        expiration_ms = resp.get("expiration")
        vendor.google_channel_expires_at = (
            datetime.fromtimestamp(int(expiration_ms) / 1000, tz=timezone.utc)
            if expiration_ms
            else now + CHANNEL_LIFETIME
        )

        if creds.token and creds.token != vendor.google_access_token:
            vendor.google_access_token = creds.token

        db.commit()
        return True
    except Exception as exc:
        logger.warning(
            "Couldn't open a Google Calendar watch channel for vendor %s: %s",
            vendor.vendor_id, exc,
        )
        return False


def handle_calendar_webhook(*, channel_id: str | None, db: Session) -> None:
    """A Google push notification arrived. Google's notification carries no
    diff — "something changed" is the whole message — so this just re-runs
    the same fetch the periodic sweep does, for the one vendor the channel
    belongs to.
    """
    if not channel_id:
        return
    vendor = db.query(Vendor).filter(Vendor.google_channel_id == channel_id).first()
    if not vendor:
        # An unknown or since-renewed channel. Google doesn't want an error
        # back for this — it just means one more notification than the
        # channel's own lifetime warranted.
        return
    refresh_busy_cache(vendor, db)


def renew_expiring_channels(*, db: Session, now: datetime | None = None) -> int:
    """Re-watch every channel expiring soon. Returns how many renewed."""
    now = now or datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=24)
    vendors = (
        db.query(Vendor)
        .filter(
            Vendor.google_access_token.isnot(None),
            Vendor.google_channel_expires_at.isnot(None),
            Vendor.google_channel_expires_at <= cutoff,
        )
        .all()
    )
    return sum(1 for vendor in vendors if watch_calendar(vendor, db, now=now))


def resync_all_connected_vendors(*, db: Session, now: datetime | None = None) -> int:
    """Re-pull busy blocks for every connected vendor, regardless of webhook
    activity — the safety net under the push channel. Returns how many
    refreshed successfully.
    """
    now = now or datetime.now(timezone.utc)
    vendors = db.query(Vendor).filter(Vendor.google_access_token.isnot(None)).all()
    return sum(1 for vendor in vendors if refresh_busy_cache(vendor, db, now=now))


# ── Availability ──────────────────────────────────────────────────────


def get_vendor_availability(
    *,
    vendor_id: str,
    start_date: str,
    end_date: str,
    db: Session,
) -> dict:
    """Compute vendor availability from baseline hours, internal bookings,
    and Google Calendar busy blocks.
    """
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise CalendarError(404, "Vendor not found")

    # 1. Baseline working hours
    base_hours = (
        db.query(VendorAvailability)
        .filter(VendorAvailability.vendor_id == vendor_id)
        .all()
    )
    day_to_hours = {av.day_of_week: (av.start_time, av.end_time) for av in base_hours}

    # 2. Internal Desiconnect bookings — the ones that actually commit the
    #    vendor. This took every booking regardless of status, so a request the
    #    vendor had declined, or one that was refunded, went on marking them
    #    busy and hiding them from search. A pending request is a lead and
    #    doesn't commit anybody either; LOCKED_BOOKING_STATUSES is the same set
    #    the approval check uses.
    from app.services.booking_service import LOCKED_BOOKING_STATUSES

    bookings = (
        db.query(Booking)
        .filter(
            Booking.vendor_id == vendor_id,
            Booking.status.in_(LOCKED_BOOKING_STATUSES),
            Booking.date_iso <= end_date[:10],
            func.coalesce(Booking.date_end, Booking.date_iso) >= start_date[:10],
        )
        .all()
    )

    internal_busy: list[tuple[datetime, datetime]] = []
    for bk in bookings:
        last_day = bk.date_end or bk.date_iso
        try:
            bk_start = datetime.fromisoformat(f"{bk.date_iso}T{bk.time_start}:00+00:00")
            bk_end = datetime.fromisoformat(f"{last_day}T{bk.time_end}:00+00:00")
            # An end at or before the start crossed midnight — the same reading
            # the conflict check and the pricing arithmetic both use.
            if bk_end <= bk_start:
                bk_end += timedelta(days=1)
        except (ValueError, TypeError):
            # No usable hours — "TBD", or a multi-day hire, which occupies its
            # days completely. Busy for the whole span rather than dropped: a
            # booking nobody can read the hours of is not a vendor who's free.
            try:
                bk_start = datetime.fromisoformat(f"{bk.date_iso}T00:00:00+00:00")
                bk_end = datetime.fromisoformat(f"{last_day}T00:00:00+00:00") + timedelta(days=1)
            except (ValueError, TypeError):
                continue
        internal_busy.append((bk_start, bk_end))

    # 3. Google Calendar busy blocks — read from the cache watch_calendar's
    #    push channel and the periodic re-sync sweep keep warm, rather than
    #    a live API call on every request the way this used to work. Falls
    #    back to a live fetch, scoped to just this request, when there's no
    #    cache yet or the request reaches past the cached window.
    google_busy: list[tuple[datetime, datetime]] = []
    google_calendar_connected = bool(vendor.google_access_token)
    google_calendar_error: str | None = None

    if vendor.google_access_token:
        req_start = datetime.fromisoformat(
            start_date if "T" in start_date else f"{start_date}T00:00:00+00:00"
        )
        req_end = datetime.fromisoformat(
            end_date if "T" in end_date else f"{end_date}T23:59:59+00:00"
        )
        synced_at = vendor.google_busy_synced_at
        cache_covers = synced_at is not None and req_end <= synced_at.replace(
            tzinfo=timezone.utc
        ) + timedelta(days=BUSY_CACHE_DAYS_AHEAD)

        if cache_covers:
            for item in vendor.google_busy_cache or []:
                try:
                    gb_start = datetime.fromisoformat(item["start"].replace("Z", "+00:00"))
                    gb_end = datetime.fromisoformat(item["end"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                if gb_end >= req_start and gb_start <= req_end:
                    google_busy.append((gb_start, gb_end))
        else:
            try:
                service, creds = create_google_calendar_service(
                    vendor.google_access_token, vendor.google_refresh_token
                )
                # Ensure full RFC3339 format — Google rejects bare dates like "2026-05-03"
                rfc_start = start_date if "T" in start_date else f"{start_date}T00:00:00Z"
                rfc_end = end_date if "T" in end_date else f"{end_date}T23:59:59Z"
                g_busy = get_freebusy_schedule(service, rfc_start, rfc_end)
                for busy in g_busy:
                    gb_start = datetime.fromisoformat(busy["start"].replace("Z", "+00:00"))
                    gb_end = datetime.fromisoformat(busy["end"].replace("Z", "+00:00"))
                    google_busy.append((gb_start, gb_end))

                # Persist refreshed token if it changed
                if creds.token and creds.token != vendor.google_access_token:
                    vendor.google_access_token = creds.token
                    db.commit()
                    logger.info("Persisted refreshed Google access token for vendor %s", vendor_id)
            except Exception as exc:
                logger.warning("Google Calendar fetch failed for vendor %s: %s", vendor_id, exc)
                google_calendar_error = f"Google Calendar error: {exc}"

    return {
        "vendor_id": vendor_id,
        "baseline_hours_map": day_to_hours,
        "internal_busy_times": [
            {"start": s.isoformat(), "end": e.isoformat()} for s, e in internal_busy
        ],
        "google_busy_times": [
            {"start": s.isoformat(), "end": e.isoformat()} for s, e in google_busy
        ],
        "google_calendar_connected": google_calendar_connected,
        "google_calendar_error": google_calendar_error,
    }
