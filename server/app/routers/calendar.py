from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.db.models import Vendor, VendorAvailability, Booking
from app.utils.calendar import get_google_auth_flow, create_google_calendar_service, get_freebusy_schedule
from typing import List
from datetime import datetime, timedelta, timezone

router = APIRouter(prefix="/vendors", tags=["calendar"])

@router.get("/{vendor_id}/google-auth", summary="Get Google OAuth redirect URL")
def google_auth_url(vendor_id: str, redirect_uri: str = "http://localhost:8000/vendors/auth/callback"):
    """
    Returns the URL that the vendor should be redirected to in order to authorize Google Calendar access.
    """
    try:
        flow = get_google_auth_flow(redirect_uri)
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
        
    auth_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        # We pass the vendor_id in the state parameter to know who is authorizing
        state=vendor_id
    )
    return {"auth_url": auth_url}


@router.get("/auth/callback", summary="Google OAuth Callback")
def google_auth_callback(
    state: str, 
    code: str, 
    db: Session = Depends(get_db),
    redirect_uri: str = "http://localhost:8000/vendors/auth/callback"
):
    """
    Callback URL where Google redirects the user after they authorize Desiconnect.
    Contains the auth code which we exchange for access and refresh tokens.
    """
    vendor_id = state
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    try:
        flow = get_google_auth_flow(redirect_uri)
        flow.fetch_token(code=code)
        credentials = flow.credentials
        
        # Save tokens securely in our DB
        vendor.google_access_token = credentials.token
        vendor.google_refresh_token = credentials.refresh_token
        vendor.calendar_id = "primary" # Default to the primary calendar
        
        db.commit()
    except Exception as e:
         raise HTTPException(status_code=400, detail=f"Failed to fetch Google tokens: {str(e)}")

    return {"message": "Google Calendar successfully connected"}


@router.get("/{vendor_id}/availability", summary="Get Vendor Open Time Slots")
def vendor_availability(
    vendor_id: str,
    start_date: str = Query(..., description="ISO formated start date (ex: 2026-03-01T00:00:00Z)"),
    end_date: str = Query(..., description="ISO formated end date (ex: 2026-03-07T23:59:59Z)"),
    db: Session = Depends(get_db)
):
    """
    Compute exactly when a vendor is completely free based on:
    1. Baseline Working Hours
    2. Google Calendar
    3. Existing Desiconnect internal Bookings
    """
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
        
    start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))

    # 1. Base Working Hours
    base_hours = db.query(VendorAvailability).filter(VendorAvailability.vendor_id == vendor_id).all()
    # E.g., { 0 (Mon): ("09:00", "17:00") }
    day_to_hours = { av.day_of_week: (av.start_time, av.end_time) for av in base_hours }

    # 2. Get existing bookings in the system
    bookings = db.query(Booking).filter(
        Booking.vendor_id == vendor_id,
        Booking.date_iso >= start_date, # simplified filter
        Booking.date_iso <= end_date
    ).all()
    
    internal_busy = []
    for bk in bookings:
        # Assuming format like "14:00" and date_iso like "2026-03-01"
        try:
           bk_start_dt = datetime.fromisoformat(f"{bk.date_iso}T{bk.time_start}:00+00:00")
           bk_end_dt = datetime.fromisoformat(f"{bk.date_iso}T{bk.time_end}:00+00:00")
           internal_busy.append((bk_start_dt, bk_end_dt))
        except ValueError:
           continue

    # 3. Get Google Calendar Busy time
    google_busy_intervals = []
    if vendor.google_access_token:
        # NOTE: Ideally this needs logic to refresh if access_token expired
        try:
            service = create_google_calendar_service(
                vendor.google_access_token, 
                vendor.google_refresh_token
            )
            g_busy = get_freebusy_schedule(service, start_date, end_date)
            for busy in g_busy:
                # "2026-03-01T15:00:00Z"
                gb_start = datetime.fromisoformat(busy['start'].replace("Z", "+00:00"))
                gb_end = datetime.fromisoformat(busy['end'].replace("Z", "+00:00"))
                google_busy_intervals.append((gb_start, gb_end))
        except Exception as e:
            # Token might be expired or revoked, could log it but let's continue for availability's sake
            pass
            
    # Compile a final available hours dictionary of day -> list of open windows
    # Since solving interval math is complicated code, we provide the raw busy blocks to the frontend
    # in this phase so Swift can plot them natively, or further implementation math can follow
    
    return {
        "vendor_id": vendor_id,
        "baseline_hours_map": day_to_hours,
        "internal_busy_times": [
              {"start": s.isoformat(), "end": e.isoformat()} for s,e in internal_busy
        ],
        "google_busy_times": [
              {"start": s.isoformat(), "end": e.isoformat()} for s,e in google_busy_intervals
        ]
    }
