"""Thin router for calendar/availability endpoints — delegates to calendar_service."""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import FRONTEND_URL, GOOGLE_OAUTH_REDIRECT_URI
from app.db.database import get_db
from app.db.models import Vendor
from app.services.calendar_service import (
    CalendarError,
    decode_state,
    get_google_auth_url as svc_get_google_auth_url,
    handle_google_callback as svc_handle_google_callback,
    get_vendor_availability as svc_get_vendor_availability,
)

router = APIRouter(prefix="/vendors", tags=["calendar"])


@router.get("/{vendor_id}/google-auth", summary="Get Google OAuth redirect URL")
def google_auth_url(vendor_id: str):
    """Returns the URL the vendor should be redirected to in order to
    authorize Google Calendar access.
    """
    try:
        return svc_get_google_auth_url(
            vendor_id=vendor_id, redirect_uri=GOOGLE_OAUTH_REDIRECT_URI
        )
    except CalendarError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/auth/callback", summary="Google OAuth callback (handled server-side)")
def google_auth_callback(
    state: str,
    code: str,
    db: Session = Depends(get_db),
):
    """Google redirects the vendor here after they grant access.
    Decodes the state to recover vendor_id and code_verifier, exchanges
    the code for tokens, then redirects the browser back to the frontend.
    """
    try:
        vendor_id, code_verifier = decode_state(state)
        svc_handle_google_callback(
            vendor_id=vendor_id,
            code=code,
            redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
            code_verifier=code_verifier,
            db=db,
        )
        return RedirectResponse(
            url=f"{FRONTEND_URL}/calendar-connected?success=true&vendor_id={vendor_id}"
        )
    except CalendarError as e:
        return RedirectResponse(
            url=f"{FRONTEND_URL}/calendar-connected?success=false&error={e.detail}"
        )


@router.get("/{vendor_id}/calendar-status", summary="Check if Google Calendar is connected")
def calendar_status(vendor_id: str, db: Session = Depends(get_db)):
    """Returns whether the vendor has linked their Google Calendar."""
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return {"google_calendar_connected": bool(vendor.google_access_token)}


@router.get("/{vendor_id}/availability", summary="Get vendor open time slots")
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
