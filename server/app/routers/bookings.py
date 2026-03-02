"""Thin router for booking endpoints — delegates to booking_service."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.schemas import BookingStatus
from app.services.booking_service import (
    BookingError,
    create_booking as svc_create_booking,
    update_booking_status as svc_update_booking_status,
    get_user_bookings as svc_get_user_bookings,
    get_vendor_bookings as svc_get_vendor_bookings,
    check_in as svc_check_in,
)

router = APIRouter(prefix="/bookings", tags=["bookings"])


# ── Request schemas ───────────────────────────────────────────────────


class BookingCreate(BaseModel):
    user_id: str
    service_id: str
    event_name: str
    time_start: str  # e.g. "10:00"
    time_end: str  # e.g. "12:00"
    location: str  # Address or name
    date_iso: str  # e.g. "2026-03-01"
    venue_latitude: Optional[float] = None
    venue_longitude: Optional[float] = None


class BookingStatusUpdate(BaseModel):
    user_id: str
    is_vendor: bool
    status: BookingStatus


class CheckInRequest(BaseModel):
    user_id: str
    is_vendor: bool
    latitude: float
    longitude: float


# ── Routes ────────────────────────────────────────────────────────────


@router.post("", summary="Create a new booking request")
def create_booking(body: BookingCreate, db: Session = Depends(get_db)):
    """Client requests a service. Creates a booking with status 'pending'."""
    try:
        return svc_create_booking(
            user_id=body.user_id,
            service_id=body.service_id,
            event_name=body.event_name,
            time_start=body.time_start,
            time_end=body.time_end,
            location=body.location,
            date_iso=body.date_iso,
            venue_latitude=body.venue_latitude,
            venue_longitude=body.venue_longitude,
            db=db,
        )
    except BookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.put("/{booking_id}/status", summary="Approve, Reject, or Cancel a booking")
def update_booking_status(
    booking_id: str, body: BookingStatusUpdate, db: Session = Depends(get_db)
):
    """Update the status of a booking. Vendors can approve or reject pending requests."""
    try:
        return svc_update_booking_status(
            booking_id=booking_id,
            user_id=body.user_id,
            is_vendor=body.is_vendor,
            status=body.status,
            db=db,
        )
    except BookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/user/{user_id}", summary="Get all bookings for a client")
def get_user_bookings(user_id: str, db: Session = Depends(get_db)):
    """Fetch all bookings created by a specific client."""
    return {"bookings": svc_get_user_bookings(user_id=user_id, db=db)}


@router.get("/vendor/{vendor_id}", summary="Get all bookings for a vendor")
def get_vendor_bookings(vendor_id: str, db: Session = Depends(get_db)):
    """Fetch all bookings directed to a specific vendor."""
    return {"bookings": svc_get_vendor_bookings(vendor_id=vendor_id, db=db)}


@router.post("/{booking_id}/check-in", summary="Verify and check into an event venue")
def booking_check_in(
    booking_id: str, body: CheckInRequest, db: Session = Depends(get_db)
):
    """Verify the user is at the venue (within ~0.2 miles) and check them in."""
    try:
        return svc_check_in(
            booking_id=booking_id,
            user_id=body.user_id,
            is_vendor=body.is_vendor,
            latitude=body.latitude,
            longitude=body.longitude,
            db=db,
        )
    except BookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
