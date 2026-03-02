"""Thin router for calendar/availability endpoints — delegates to calendar_service."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.services.calendar_service import (
    CalendarError,
    get_google_auth_url as svc_get_google_auth_url,
    handle_google_callback as svc_handle_google_callback,
    get_vendor_availability as svc_get_vendor_availability,
)

router = APIRouter(prefix="/vendors", tags=["calendar"])


@router.get("/{vendor_id}/google-auth", summary="Get Google OAuth redirect URL")
def google_auth_url(
    vendor_id: str,
    redirect_uri: str = "http://localhost:8000/vendors/auth/callback",
):
    """Returns the URL the vendor should visit to authorize Google Calendar access."""
    try:
        return svc_get_google_auth_url(vendor_id=vendor_id, redirect_uri=redirect_uri)
    except CalendarError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/auth/callback", summary="Google OAuth Callback")
def google_auth_callback(
    state: str,
    code: str,
    db: Session = Depends(get_db),
    redirect_uri: str = "http://localhost:8000/vendors/auth/callback",
):
    """Callback where Google redirects the user after authorizing Desiconnect."""
    try:
        return svc_handle_google_callback(
            vendor_id=state, code=code, redirect_uri=redirect_uri, db=db
        )
    except CalendarError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/{vendor_id}/availability", summary="Get Vendor Open Time Slots")
def vendor_availability(
    vendor_id: str,
    start_date: str = Query(
        ..., description="ISO formatted start date (e.g. 2026-03-01T00:00:00Z)"
    ),
    end_date: str = Query(
        ..., description="ISO formatted end date (e.g. 2026-03-07T23:59:59Z)"
    ),
    db: Session = Depends(get_db),
):
    """Compute when a vendor is free based on baseline hours, Google Calendar,
    and existing Desiconnect bookings.
    """
    try:
        return svc_get_vendor_availability(
            vendor_id=vendor_id, start_date=start_date, end_date=end_date, db=db
        )
    except CalendarError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
