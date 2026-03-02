from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.db.models import Booking, User, Vendor, Service
from pydantic import BaseModel
from typing import List, Optional
import uuid
from datetime import datetime, timezone
from app.models.schemas import BookingStatus
from app.utils.location import calculate_distance_miles

router = APIRouter(prefix="/bookings", tags=["bookings"])

class BookingCreate(BaseModel):
    user_id: str
    service_id: str
    event_name: str
    time_start: str # e.g. "10:00"
    time_end: str # e.g. "12:00"
    location: str # Address or name
    date_iso: str # e.g. "2026-03-01"
    venue_latitude: Optional[float] = None
    venue_longitude: Optional[float] = None

class BookingStatusUpdate(BaseModel):
    user_id: str
    is_vendor: bool
    status: BookingStatus # Use the Enum from schemas.py

class CheckInRequest(BaseModel):
    user_id: str
    is_vendor: bool
    latitude: float
    longitude: float

@router.post("", summary="Create a new booking request")
def create_booking(body: BookingCreate, db: Session = Depends(get_db)):
    """
    Client requests a service. Creates a booking with status 'pending'.
    """
    service = db.query(Service).filter(Service.service_id == body.service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
        
    booking_id = str(uuid.uuid4())
    booking = Booking(
        booking_id=booking_id,
        user_id=body.user_id,
        vendor_id=service.vendor_id,
        service_id=body.service_id,
        event_name=body.event_name,
        time_start=body.time_start,
        time_end=body.time_end,
        location=body.location,
        date_iso=body.date_iso,
        venue_latitude=body.venue_latitude,
        venue_longitude=body.venue_longitude,
        status=BookingStatus.PENDING.value
    )
    
    db.add(booking)
    db.commit()
    db.refresh(booking)
    
    # FUTURE: Dispatch a Push Notification to the Vendor here using Firebase.
    
    return {"message": "Booking requested successfully", "booking_id": booking.booking_id, "status": booking.status}


@router.put("/{booking_id}/status", summary="Approve, Reject, or Cancel a booking")
def update_booking_status(booking_id: str, body: BookingStatusUpdate, db: Session = Depends(get_db)):
    """
    Update the status of a booking to an accepted schema status.
    Vendors can approve or reject pending requests.
    """
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
        
    status_str = body.status.value
        
    if body.is_vendor:
        vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
        if not vendor or vendor.user_id != body.user_id:
            raise HTTPException(status_code=403, detail="Unauthorized: Not the vendor for this booking")
    else:
        if booking.user_id != body.user_id:
            raise HTTPException(status_code=403, detail="Unauthorized: Not the client who created this booking")
            
    # Business logic validation
    if status_str in [BookingStatus.APPROVED.value, BookingStatus.REJECTED.value]:
        if not body.is_vendor:
            raise HTTPException(status_code=403, detail="Only vendors can approve or reject a booking")
        if booking.status != BookingStatus.PENDING.value:
            raise HTTPException(status_code=400, detail=f"Cannot change status from {booking.status} to {status_str}")
            
    booking.status = status_str
    db.commit()
    db.refresh(booking)
    
    # FUTURE: Dispatch Push Notification to the other party using Firebase.
    
    return {"message": f"Booking successfully updated to {status_str}", "booking_id": booking.booking_id}


@router.get("/user/{user_id}", summary="Get all bookings for a client")
def get_user_bookings(user_id: str, db: Session = Depends(get_db)):
    """Fetch all bookings created by a specific client."""
    bookings = db.query(Booking).filter(Booking.user_id == user_id).all()
    return {"bookings": bookings}


@router.get("/vendor/{vendor_id}", summary="Get all bookings for a vendor")
def get_vendor_bookings(vendor_id: str, db: Session = Depends(get_db)):
    """Fetch all bookings directed to a specific vendor."""
    bookings = db.query(Booking).filter(Booking.vendor_id == vendor_id).all()
    return {"bookings": bookings}


@router.post("/{booking_id}/check-in", summary="Verify and check into an event venue")
def booking_check_in(
    booking_id: str,
    body: CheckInRequest,
    db: Session = Depends(get_db)
):
    """
    Verify the user is at the venue (within ~0.2 miles) and successfully check them in.
    """
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
        
    if booking.venue_latitude is None or booking.venue_longitude is None:
        raise HTTPException(status_code=400, detail="Booking has no venue coordinates set")
        
    distance = calculate_distance_miles(
        body.latitude, body.longitude, 
        booking.venue_latitude, booking.venue_longitude
    )
    
    # Allow a margin of error of 0.2 miles (approx 1036 feet)
    if distance > 0.2:
        raise HTTPException(
            status_code=400, 
            detail=f"You must be at the venue to check in. You are currently {round(distance, 2)} miles away."
        )
        
    current_time = datetime.now(timezone.utc).isoformat()
    
    if body.is_vendor:
        # Verify the user checking in is actually the vendor for this booking
        vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
        if not vendor or vendor.user_id != body.user_id:
             raise HTTPException(status_code=403, detail="Unauthorized vendor")
        booking.vendor_checked_in_at = current_time
    else:
        # Verify the user checking in is exactly the client who booked it
        if booking.user_id != body.user_id:
             raise HTTPException(status_code=403, detail="Unauthorized user")
        booking.client_checked_in_at = current_time
        
    db.commit()
    
    return {
        "message": "Check-in successful", 
        "distance_miles": round(distance, 2), 
        "check_in_time": current_time
    }
