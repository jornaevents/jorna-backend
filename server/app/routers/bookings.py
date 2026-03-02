from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.db.models import Booking, User, Vendor, Service
from pydantic import BaseModel
from typing import List, Optional
import uuid
import logging
from datetime import datetime, timezone
from app.models.schemas import BookingStatus
from app.utils.location import calculate_distance_miles
from app.utils.notifications import notify_booking_status_change, notify_check_in

logger = logging.getLogger(__name__)

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
    
    # --- Push Notification: tell the vendor about the new request ---
    client = db.query(User).filter(User.user_id == body.user_id).first()
    vendor_obj = db.query(Vendor).filter(Vendor.vendor_id == service.vendor_id).first()
    vendor_user = db.query(User).filter(User.user_id == vendor_obj.user_id).first() if vendor_obj else None

    notification_result = notify_booking_status_change(
        status=BookingStatus.PENDING.value,
        booking_id=booking.booking_id,
        event_name=body.event_name,
        service_name=service.name,
        client_name=f"{client.f_name} {client.l_name}" if client else "Client",
        vendor_name=f"{vendor_user.f_name} {vendor_user.l_name}" if vendor_user else "Vendor",
        client_fcm_token=client.fcm_token if client else None,
        vendor_fcm_token=vendor_user.fcm_token if vendor_user else None,
    )
    logger.info("Booking %s notification result: %s", booking.booking_id, notification_result)
    
    return {
        "message": "Booking requested successfully",
        "booking_id": booking.booking_id,
        "status": booking.status,
        "notification": notification_result,
    }


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
    
    # --- Push Notification: tell the other party about the status change ---
    client = db.query(User).filter(User.user_id == booking.user_id).first()
    vendor_obj = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    vendor_user = db.query(User).filter(User.user_id == vendor_obj.user_id).first() if vendor_obj else None
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()

    notification_result = notify_booking_status_change(
        status=status_str,
        booking_id=booking.booking_id,
        event_name=booking.event_name,
        service_name=service.name if service else "Service",
        client_name=f"{client.f_name} {client.l_name}" if client else "Client",
        vendor_name=f"{vendor_user.f_name} {vendor_user.l_name}" if vendor_user else "Vendor",
        client_fcm_token=client.fcm_token if client else None,
        vendor_fcm_token=vendor_user.fcm_token if vendor_user else None,
    )
    logger.info("Booking %s status→%s notification: %s", booking.booking_id, status_str, notification_result)
    
    return {
        "message": f"Booking successfully updated to {status_str}",
        "booking_id": booking.booking_id,
        "notification": notification_result,
    }


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
    
    # --- Push Notification: tell the OTHER party that someone checked in ---
    client = db.query(User).filter(User.user_id == booking.user_id).first()
    vendor_obj = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    vendor_user = db.query(User).filter(User.user_id == vendor_obj.user_id).first() if vendor_obj else None

    # Notify the opposite party
    recipient_token = (
        client.fcm_token if (body.is_vendor and client) else
        (vendor_user.fcm_token if vendor_user else None)
    )
    checkin_notification = notify_check_in(
        booking_id=booking.booking_id,
        event_name=booking.event_name,
        is_vendor=body.is_vendor,
        client_name=f"{client.f_name} {client.l_name}" if client else "Client",
        vendor_name=f"{vendor_user.f_name} {vendor_user.l_name}" if vendor_user else "Vendor",
        recipient_fcm_token=recipient_token,
    )
    logger.info("Check-in notification for booking %s: %s", booking.booking_id, checkin_notification)
    
    return {
        "message": "Check-in successful", 
        "distance_miles": round(distance, 2), 
        "check_in_time": current_time,
        "notification": checkin_notification,
    }
