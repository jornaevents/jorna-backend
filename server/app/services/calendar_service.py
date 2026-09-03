"""Business logic for Google Calendar OAuth and vendor availability."""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timedelta

# google_auth_oauthlib raises an error if Google returns extra scopes (e.g. openid,
# userinfo.email) beyond what was explicitly requested. Relaxing this is safe here
# because we only use the calendar scope — the extra ones come from Google automatically.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import SECRET_KEY
from app.db.models import Vendor, VendorAvailability, Booking
from app.utils.calendar import (
    get_google_auth_flow,
    create_google_calendar_service,
    get_freebusy_schedule,
)

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

    # 3. Google Calendar busy blocks
    google_busy: list[tuple[datetime, datetime]] = []
    google_calendar_connected = bool(vendor.google_access_token)
    google_calendar_error: str | None = None

    if vendor.google_access_token:
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
