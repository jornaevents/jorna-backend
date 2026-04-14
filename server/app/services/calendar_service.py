"""Business logic for Google Calendar OAuth and vendor availability."""

import base64
import json
import logging
import secrets
from datetime import datetime

from sqlalchemy.orm import Session

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


def _encode_state(vendor_id: str, code_verifier: str) -> str:
    """Pack vendor_id + code_verifier into a base64 string for the OAuth state param."""
    payload = json.dumps({"vendor_id": vendor_id, "code_verifier": code_verifier})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_state(state: str) -> tuple[str, str]:
    """Decode the OAuth state param back into (vendor_id, code_verifier).
    Raises CalendarError 400 if the state is malformed.
    """
    try:
        payload = json.loads(base64.urlsafe_b64decode(state.encode()).decode())
        return payload["vendor_id"], payload["code_verifier"]
    except Exception:
        raise CalendarError(400, "Invalid OAuth state parameter")


# ── OAuth flow ────────────────────────────────────────────────────────


def get_google_auth_url(*, vendor_id: str, redirect_uri: str) -> dict:
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

    state = _encode_state(vendor_id, code_verifier)

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

        db.commit()
    except Exception as e:
        raise CalendarError(400, f"Failed to fetch Google tokens: {str(e)}")

    return {"message": "Google Calendar successfully connected"}


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

    # 2. Internal Desiconnect bookings
    bookings = (
        db.query(Booking)
        .filter(
            Booking.vendor_id == vendor_id,
            Booking.date_iso >= start_date,
            Booking.date_iso <= end_date,
        )
        .all()
    )

    internal_busy: list[tuple[datetime, datetime]] = []
    for bk in bookings:
        try:
            bk_start = datetime.fromisoformat(f"{bk.date_iso}T{bk.time_start}:00+00:00")
            bk_end = datetime.fromisoformat(f"{bk.date_iso}T{bk.time_end}:00+00:00")
            internal_busy.append((bk_start, bk_end))
        except ValueError:
            continue

    # 3. Google Calendar busy blocks
    google_busy: list[tuple[datetime, datetime]] = []
    google_calendar_connected = bool(vendor.google_access_token)
    google_calendar_error: str | None = None

    if vendor.google_access_token:
        try:
            service, creds = create_google_calendar_service(
                vendor.google_access_token, vendor.google_refresh_token
            )
            g_busy = get_freebusy_schedule(service, start_date, end_date)
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
            google_calendar_error = "Google Calendar data unavailable — the vendor may need to reconnect their account."

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
