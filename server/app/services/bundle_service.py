"""Business logic for event bundles — grouping multiple bookings under one event."""

import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.db.models import Booking, Bundle, Event, Service, User, Vendor

logger = logging.getLogger(__name__)


class BundleError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ── Helpers ───────────────────────────────────────────────────────────


def _booking_summary(booking: Booking, db: Session) -> dict:
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    vendor_user = db.query(User).filter(User.user_id == vendor.user_id).first() if vendor else None
    price = (booking.amount_cents / 100) if booking.amount_cents else (service.price if service else 0.0)
    return {
        "booking_id": booking.booking_id,
        "event_name": booking.event_name,
        "status": booking.status,
        "payment_status": booking.payment_status,
        "date_iso": booking.date_iso,
        "time_start": booking.time_start,
        "time_end": booking.time_end,
        "location": booking.location,
        "service_name": service.name if service else None,
        "service_category": service.category if service else None,
        "vendor_name": f"{vendor_user.f_name} {vendor_user.l_name}" if vendor_user else None,
        "vendor_id": booking.vendor_id,
        "price": price,
        "amount_cents": booking.amount_cents,
    }


def _bundle_dict(bundle: Bundle, bookings: list[Booking], db: Session) -> dict:
    booking_summaries = [_booking_summary(b, db) for b in bookings]
    total_cost = sum(b["price"] for b in booking_summaries)

    status_counts: dict[str, int] = {}
    for b in booking_summaries:
        status_counts[b["status"]] = status_counts.get(b["status"], 0) + 1

    event = db.query(Event).filter(Event.event_id == bundle.event_id).first() if bundle.event_id else None

    return {
        "bundle_id": bundle.bundle_id,
        "user_id": bundle.user_id,
        "name": bundle.name,
        "status": bundle.status,
        "event_id": bundle.event_id,
        "event": {
            "event_id": event.event_id,
            "name": event.name,
            "date_iso": event.date_iso,
            "location": event.location,
            "event_type": event.event_type,
            "guest_count": event.guest_count,
            "budget": event.budget,
        } if event else None,
        "bookings": booking_summaries,
        "booking_count": len(booking_summaries),
        "total_estimated_cost": round(total_cost, 2),
        "status_breakdown": status_counts,
        "created_at": bundle.created_at.isoformat(),
        "updated_at": bundle.updated_at.isoformat(),
    }


def _assert_owns_booking(booking: Booking, caller_user_id: str) -> None:
    if booking.user_id != caller_user_id:
        raise BundleError(403, "You can only add your own bookings to a bundle")


def _assert_owns_bundle(bundle: Bundle, caller_user_id: str) -> None:
    if bundle.user_id != caller_user_id:
        raise BundleError(403, "You do not own this bundle")


# ── Service functions ─────────────────────────────────────────────────


def create_bundle(
    *,
    user_id: str,
    name: str,
    event_id: str | None,
    booking_ids: list[str],
    db: Session,
) -> dict:
    """Create a bundle, optionally linking an event and existing bookings."""
    if event_id:
        event = db.query(Event).filter(Event.event_id == event_id).first()
        if not event:
            raise BundleError(404, "Event not found")
        if event.user_id != user_id:
            raise BundleError(403, "You do not own this event")

    now = datetime.now(timezone.utc)
    bundle = Bundle(
        user_id=user_id,
        event_id=event_id,
        name=name,
        status="draft",
        created_at=now,
        updated_at=now,
    )
    db.add(bundle)
    db.flush()

    bookings = []
    for bid in booking_ids:
        booking = db.query(Booking).filter(Booking.booking_id == bid).first()
        if not booking:
            raise BundleError(404, f"Booking {bid} not found")
        _assert_owns_booking(booking, user_id)
        if booking.bundle_id:
            raise BundleError(400, f"Booking {bid} already belongs to another bundle")
        booking.bundle_id = bundle.bundle_id
        bookings.append(booking)

    db.commit()
    return _bundle_dict(bundle, bookings, db)


def get_bundle(*, bundle_id: str, caller_user_id: str, db: Session) -> dict:
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise BundleError(404, "Bundle not found")
    _assert_owns_bundle(bundle, caller_user_id)
    bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    return _bundle_dict(bundle, bookings, db)


def list_bundles(*, user_id: str, db: Session) -> list[dict]:
    bundles = db.query(Bundle).filter(Bundle.user_id == user_id).order_by(Bundle.created_at.desc()).all()
    result = []
    for b in bundles:
        bookings = db.query(Booking).filter(Booking.bundle_id == b.bundle_id).all()
        result.append(_bundle_dict(b, bookings, db))
    return result


def add_booking_to_bundle(*, bundle_id: str, booking_id: str, caller_user_id: str, db: Session) -> dict:
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise BundleError(404, "Bundle not found")
    _assert_owns_bundle(bundle, caller_user_id)

    if bundle.status == "cancelled":
        raise BundleError(400, "Cannot add bookings to a cancelled bundle")

    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise BundleError(404, "Booking not found")
    _assert_owns_booking(booking, caller_user_id)

    if booking.bundle_id == bundle_id:
        raise BundleError(400, "Booking is already in this bundle")
    if booking.bundle_id:
        raise BundleError(400, "Booking already belongs to another bundle")

    booking.bundle_id = bundle_id
    bundle.updated_at = datetime.now(timezone.utc)
    db.commit()

    bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    return _bundle_dict(bundle, bookings, db)


def remove_booking_from_bundle(*, bundle_id: str, booking_id: str, caller_user_id: str, db: Session) -> dict:
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise BundleError(404, "Bundle not found")
    _assert_owns_bundle(bundle, caller_user_id)

    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise BundleError(404, "Booking not found")
    if booking.bundle_id != bundle_id:
        raise BundleError(400, "Booking is not in this bundle")

    booking.bundle_id = None
    bundle.updated_at = datetime.now(timezone.utc)
    db.commit()

    bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    return _bundle_dict(bundle, bookings, db)


def update_bundle_status(*, bundle_id: str, status: str, caller_user_id: str, db: Session) -> dict:
    valid = {"draft", "active", "completed", "cancelled"}
    if status not in valid:
        raise BundleError(400, f"Status must be one of: {', '.join(sorted(valid))}")

    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise BundleError(404, "Bundle not found")
    _assert_owns_bundle(bundle, caller_user_id)

    bundle.status = status
    bundle.updated_at = datetime.now(timezone.utc)
    db.commit()

    bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    return _bundle_dict(bundle, bookings, db)


def delete_bundle(*, bundle_id: str, caller_user_id: str, db: Session) -> None:
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise BundleError(404, "Bundle not found")
    _assert_owns_bundle(bundle, caller_user_id)

    # Detach bookings — they remain active, just no longer part of this bundle
    db.query(Booking).filter(Booking.bundle_id == bundle_id).update({"bundle_id": None})
    db.delete(bundle)
    db.commit()
