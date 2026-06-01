"""Business logic for bookings: create, status update, fetch, check-in."""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Booking, User, Vendor, Service
from app.models.schemas import BookingStatus
from app.utils.location import calculate_distance_miles
from app.utils.notifications import notify_booking_status_change, notify_check_in

logger = logging.getLogger(__name__)

# ── Exceptions ────────────────────────────────────────────────────────

class BookingError(Exception):
    """Raised when a booking operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ── Helpers ───────────────────────────────────────────────────────────

def _get_booking_parties(db: Session, booking: Booking):
    """Return (client_user, vendor_obj, vendor_user, service) for a booking."""
    client = db.query(User).filter(User.user_id == booking.user_id).first()
    vendor_obj = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    vendor_user = (
        db.query(User).filter(User.user_id == vendor_obj.user_id).first()
        if vendor_obj
        else None
    )
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    return client, vendor_obj, vendor_user, service


def _dispatch_status_notification(
    status: str,
    booking: Booking,
    client: User | None,
    vendor_user: User | None,
    service: Service | None,
) -> dict:
    """Send push notifications for a booking status change."""
    result = notify_booking_status_change(
        status=status,
        booking_id=booking.booking_id,
        event_name=booking.event_name,
        service_name=service.name if service else "Service",
        client_name=f"{client.f_name} {client.l_name}" if client else "Client",
        vendor_name=f"{vendor_user.f_name} {vendor_user.l_name}" if vendor_user else "Vendor",
        client_fcm_token=client.fcm_token if client else None,
        vendor_fcm_token=vendor_user.fcm_token if vendor_user else None,
    )
    logger.info("Booking %s status→%s notification: %s", booking.booking_id, status, result)
    return result


# ── Service functions ─────────────────────────────────────────────────

def create_booking(
    *,
    user_id: str,
    service_id: str,
    event_name: str,
    time_start: str,
    time_end: str,
    location: str,
    date_iso: str,
    venue_latitude: float | None,
    venue_longitude: float | None,
    db: Session,
) -> dict:
    """Create a new booking and notify the vendor."""
    service = db.query(Service).filter(Service.service_id == service_id).first()
    if not service:
        raise BookingError(404, "Service not found")

    booking = Booking(
        booking_id=str(uuid.uuid4()),
        user_id=user_id,
        vendor_id=service.vendor_id,
        service_id=service_id,
        event_name=event_name,
        time_start=time_start,
        time_end=time_end,
        location=location,
        date_iso=date_iso,
        venue_latitude=venue_latitude,
        venue_longitude=venue_longitude,
        status=BookingStatus.PENDING.value,
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    client, vendor_obj, vendor_user, _ = _get_booking_parties(db, booking)
    try:
        notification = _dispatch_status_notification(
            BookingStatus.PENDING.value, booking, client, vendor_user, service
        )
    except Exception as exc:
        logger.warning("Notification failed for booking %s: %s", booking.booking_id, exc)
        notification = {"error": "Notification unavailable"}

    return {
        "message": "Booking requested successfully",
        "booking_id": booking.booking_id,
        "status": booking.status,
        "notification": notification,
    }


def update_booking_status(
    *,
    booking_id: str,
    caller_user_id: str,
    status: BookingStatus,
    db: Session,
) -> dict:
    """Validate authorization + business rules, then update status."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise BookingError(404, "Booking not found")

    status_str = status.value

    # Derive role server-side — never trust a client-supplied flag.
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    is_vendor = vendor is not None and vendor.user_id == caller_user_id
    is_customer = booking.user_id == caller_user_id

    if not is_vendor and not is_customer:
        raise BookingError(403, "You are not a party to this booking")

    # Business rules
    _negotiable = {BookingStatus.PENDING.value, BookingStatus.NEGOTIATION_ONGOING.value}

    if status_str == BookingStatus.NEGOTIATION_ONGOING.value:
        if booking.status not in _negotiable:
            raise BookingError(
                400, f"Cannot move to negotiation from {booking.status}"
            )

    if status_str in [BookingStatus.APPROVED.value, BookingStatus.REJECTED.value]:
        if not is_vendor:
            raise BookingError(403, "Only vendors can approve or reject a booking")
        if booking.status not in _negotiable:
            raise BookingError(
                400, f"Cannot change status from {booking.status} to {status_str}"
            )

    booking.status = status_str
    if status_str == BookingStatus.APPROVED.value:
        booking.confirmed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(booking)

    # Auto-create a draft bundle when a booking is first approved
    if status_str == BookingStatus.APPROVED.value:
        try:
            from app.db.models import Bundle
            existing_draft = db.query(Bundle).filter(
                Bundle.user_id == booking.user_id,
                Bundle.status == "draft",
            ).first()
            if not existing_draft:
                from app.services.bundle_service import create_bundle
                service_name = service.name if service else "Booking"
                create_bundle(
                    user_id=booking.user_id,
                    name=f"{booking.event_name} Bundle",
                    event_id=None,
                    booking_ids=[booking.booking_id],
                    db=db,
                )
                logger.info("Auto-created draft bundle for booking %s", booking.booking_id)
            elif not booking.bundle_id:
                booking.bundle_id = existing_draft.bundle_id
                existing_draft.updated_at = datetime.now(timezone.utc)
                db.commit()
                logger.info("Added booking %s to existing draft bundle", booking.booking_id)
        except Exception as exc:
            logger.warning("Failed to auto-create bundle for booking %s: %s", booking.booking_id, exc)

    client, vendor_obj, vendor_user, service = _get_booking_parties(db, booking)
    try:
        notification = _dispatch_status_notification(
            status_str, booking, client, vendor_user, service
        )
    except Exception as exc:
        logger.warning("Notification failed for booking %s: %s", booking.booking_id, exc)
        notification = {"error": "Notification unavailable"}

    return {
        "message": f"Booking successfully updated to {status_str}",
        "booking_id": booking.booking_id,
        "notification": notification,
    }


def get_booking(*, booking_id: str, caller_user_id: str, db: Session) -> Booking:
    """Return a single booking. Caller must be the client or the vendor."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise BookingError(404, "Booking not found")
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    is_vendor = vendor is not None and vendor.user_id == caller_user_id
    is_customer = booking.user_id == caller_user_id
    if not is_vendor and not is_customer:
        raise BookingError(403, "You are not a party to this booking")
    return booking


def get_user_bookings(*, user_id: str, limit: int = 20, offset: int = 0, db: Session) -> dict:
    """Return a paginated list of bookings created by a client."""
    query = db.query(Booking).filter(Booking.user_id == user_id)
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_vendor_bookings(*, vendor_id: str, caller_user_id: str, limit: int = 20, offset: int = 0, db: Session) -> dict:
    """Return a paginated list of bookings directed to a vendor."""
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise BookingError(404, "Vendor not found")
    if vendor.user_id != caller_user_id:
        raise BookingError(403, "You are not authorised to view these bookings")
    query = db.query(Booking).filter(Booking.vendor_id == vendor_id)
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def check_in(
    *,
    booking_id: str,
    caller_user_id: str,
    latitude: float,
    longitude: float,
    db: Session,
) -> dict:
    """Verify GPS proximity and record check-in."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise BookingError(404, "Booking not found")

    if booking.venue_latitude is None or booking.venue_longitude is None:
        raise BookingError(400, "Booking has no venue coordinates set")

    distance = calculate_distance_miles(
        latitude, longitude, booking.venue_latitude, booking.venue_longitude
    )
    if distance > 0.2:
        raise BookingError(
            400,
            f"You must be at the venue to check in. You are currently {round(distance, 2)} miles away.",
        )

    # Derive role server-side — never trust a client-supplied flag.
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    is_vendor = vendor is not None and vendor.user_id == caller_user_id
    is_customer = booking.user_id == caller_user_id

    if not is_vendor and not is_customer:
        raise BookingError(403, "You are not a party to this booking")

    current_time = datetime.now(timezone.utc).isoformat()

    if is_vendor:
        booking.vendor_checked_in_at = current_time
    else:
        booking.client_checked_in_at = current_time

    db.commit()

    # Notify the other party
    client, vendor_obj, vendor_user, _ = _get_booking_parties(db, booking)
    recipient_token = (
        client.fcm_token
        if (is_vendor and client)
        else (vendor_user.fcm_token if vendor_user else None)
    )
    checkin_notification = notify_check_in(
        booking_id=booking.booking_id,
        event_name=booking.event_name,
        is_vendor=is_vendor,
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
