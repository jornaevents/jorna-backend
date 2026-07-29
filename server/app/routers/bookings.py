"""Thin router for booking endpoints — delegates to booking_service."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from typing import Optional
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User, Vendor
from app.dependencies import get_current_user
from app.limiter import limiter
from app.models.schemas import BookingStatus
from app.services.booking_service import (
    BookingError,
    create_booking as svc_create_booking,
    get_booking as svc_get_booking,
    update_booking as svc_update_booking,
    update_booking_status as svc_update_booking_status,
    get_user_bookings as svc_get_user_bookings,
    get_vendor_bookings as svc_get_vendor_bookings,
    check_in as svc_check_in,
)

router = APIRouter(prefix="/bookings", tags=["bookings"])


# ── Request schemas ───────────────────────────────────────────────────


class BookingCreate(BaseModel):
    service_id: str
    event_name: str
    time_start: str  # e.g. "10:00"
    time_end: str    # e.g. "12:00"
    location: str
    date_iso: str    # e.g. "2026-03-01"
    date_end: Optional[str] = None    # e.g. "2026-03-03" for a multi-day event
    guest_count: Optional[int] = None  # lets per-person services price rate x guests
    venue_latitude: Optional[float] = None
    venue_longitude: Optional[float] = None
    bundle_id: Optional[str] = Field(default=None, examples=[None])


class BookingUpdate(BaseModel):
    date_iso: Optional[str] = None
    date_end: Optional[str] = None
    guest_count: Optional[int] = None
    time_start: Optional[str] = None
    time_end: Optional[str] = None
    location: Optional[str] = None
    venue_latitude: Optional[float] = None
    venue_longitude: Optional[float] = None


class BookingStatusUpdate(BaseModel):
    status: BookingStatus


class CheckInRequest(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)


# ── Routes ────────────────────────────────────────────────────────────


@router.post("", summary="Create a new booking request")
@limiter.limit("10/minute")
def create_booking(
    request: Request,
    body: BookingCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Client requests a service. Creates a booking with status 'pending'."""
    try:
        return svc_create_booking(
            user_id=current_user.user_id,
            service_id=body.service_id,
            event_name=body.event_name,
            time_start=body.time_start,
            time_end=body.time_end,
            location=body.location,
            date_iso=body.date_iso,
            date_end=body.date_end,
            guest_count=body.guest_count,
            venue_latitude=body.venue_latitude,
            venue_longitude=body.venue_longitude,
            bundle_id=body.bundle_id,
            db=db,
        )
    except BookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/{booking_id}", summary="Get a single booking")
def get_booking_route(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Fetch a single booking by ID. Caller must be the client or the vendor."""
    try:
        return svc_get_booking(booking_id=booking_id, caller_user_id=current_user.user_id, db=db)
    except BookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.patch("/{booking_id}", summary="Update booking date, time, or location")
def update_booking_route(
    booking_id: str,
    body: BookingUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Update mutable fields on a booking (date, time, location, venue coordinates).
    Only the client who created the booking can call this, and only while the
    booking is pending or under negotiation."""
    try:
        return svc_update_booking(
            booking_id=booking_id,
            caller_user_id=current_user.user_id,
            update_data=body.model_dump(exclude_unset=True),
            db=db,
        )
    except BookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.put("/{booking_id}/status", summary="Approve, Reject, or Cancel a booking")
def update_booking_status(
    booking_id: str,
    body: BookingStatusUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Update the status of a booking. Vendors can approve or reject pending requests."""
    try:
        return svc_update_booking_status(
            booking_id=booking_id,
            caller_user_id=current_user.user_id,
            status=body.status,
            db=db,
        )
    except BookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/user/{user_id}", summary="Get all bookings for a client")
def get_user_bookings(
    user_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Fetch bookings created by a specific client. Users can only fetch their own."""
    if current_user.user_id != user_id:
        raise HTTPException(status_code=403, detail="You can only view your own bookings")
    return svc_get_user_bookings(user_id=user_id, limit=limit, offset=offset, db=db)


@router.get("/vendor", summary="Get all bookings for the authenticated vendor")
def get_vendor_bookings_route(
    vendor_id: Optional[str] = Query(None, description="Vendor ID to filter by"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Fetch bookings for the authenticated vendor. If vendor_id is omitted, use the current user's vendor profile."""
    if vendor_id is None:
        vendor = db.query(Vendor).filter(Vendor.user_id == current_user.user_id).first()
        if not vendor:
            raise HTTPException(status_code=403, detail="You must be a vendor to view vendor bookings")
        vendor_id = vendor.vendor_id
    return svc_get_vendor_bookings(vendor_id=vendor_id, caller_user_id=current_user.user_id, limit=limit, offset=offset, db=db)


@router.get("/vendor/{vendor_id}", summary="Get all bookings for a vendor")
def get_vendor_bookings_by_id_route(
    vendor_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Fetch bookings directed to a specific vendor. Only the vendor themselves can access this."""
    try:
        return svc_get_vendor_bookings(vendor_id=vendor_id, caller_user_id=current_user.user_id, limit=limit, offset=offset, db=db)
    except BookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post(
    "/{booking_id}/resend-checkin",
    summary="Send this vendor their check-in email again",
)
@limiter.limit("10/minute")
def resend_checkin(
    request: Request,
    booking_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """The client's nudge, for one booking.

    Per booking rather than per plan, because that's the shape of the problem:
    four vendors are here and the fifth hasn't checked in. Sending all five
    another email to reach one of them is how a marketplace teaches its vendors
    to filter its mail.

    The cooldown lives in resend_state alongside every other condition, so this
    and the button the client sees agree by construction.
    """
    from app.services.reminder_service import (
        ReminderError,
        resend_checkin_reminder as svc_resend,
    )

    try:
        return svc_resend(booking_id=booking_id, user_id=current_user.user_id, db=db)
    except ReminderError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{booking_id}/check-in", summary="Verify and check into an event venue")
def booking_check_in(
    booking_id: str,
    body: CheckInRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Verify the user is at the venue (within ~0.2 miles) and check them in."""
    try:
        return svc_check_in(
            booking_id=booking_id,
            caller_user_id=current_user.user_id,
            latitude=body.latitude,
            longitude=body.longitude,
            db=db,
        )
    except BookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
