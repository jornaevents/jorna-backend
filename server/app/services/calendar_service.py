"""Business logic for Google Calendar OAuth and vendor availability."""

import logging
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


def get_google_auth_url(*, vendor_id: str, redirect_uri: str) -> dict:
    """Build a Google OAuth authorization URL for the vendor."""
    try:
        flow = get_google_auth_flow(redirect_uri)
    except FileNotFoundError as e:
        raise CalendarError(500, str(e))

    auth_url, _state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        state=vendor_id,
    )
    return {"auth_url": auth_url}


def handle_google_callback(
    *,
    vendor_id: str,
    code: str,
    redirect_uri: str,
    db: Session,
) -> dict:
    """Exchange the auth code for tokens and persist them."""
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise CalendarError(404, "Vendor not found")

    try:
        flow = get_google_auth_flow(redirect_uri)
        flow.fetch_token(code=code)
        credentials = flow.credentials

        vendor.google_access_token = credentials.token
        vendor.google_refresh_token = credentials.refresh_token
        vendor.calendar_id = "primary"

        db.commit()
    except Exception as e:
        raise CalendarError(400, f"Failed to fetch Google tokens: {str(e)}")

    return {"message": "Google Calendar successfully connected"}


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

            # Persist refreshed access token back to DB if it changed
            if creds.token and creds.token != vendor.google_access_token:
                vendor.google_access_token = creds.token
                db.commit()
                logger.info("Persisted refreshed Google access token for vendor %s", vendor_id)
        except Exception:
            pass  # token may be expired; continue without Google data

    return {
        "vendor_id": vendor_id,
        "baseline_hours_map": day_to_hours,
        "internal_busy_times": [
            {"start": s.isoformat(), "end": e.isoformat()} for s, e in internal_busy
        ],
        "google_busy_times": [
            {"start": s.isoformat(), "end": e.isoformat()} for s, e in google_busy
        ],
    }
