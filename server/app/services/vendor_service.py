"""Business logic for vendor profiles, CRUD, and search / discovery."""

from sqlalchemy.orm import Session

from app.db.models import Vendor, Service, User
from app.utils.location import calculate_distance_miles


class VendorError(Exception):
    """Raised when a vendor operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def create_vendor(*, user_id: str, bio: str, db: Session) -> dict:
    """Create a vendor profile for *user_id*. Raises 400 if one already exists."""
    existing = db.query(Vendor).filter(Vendor.user_id == user_id).first()
    if existing:
        raise VendorError(400, "You already have a vendor profile")
    vendor = Vendor(user_id=user_id, bio=bio, rating=0.0, num_events=0)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return {
        "vendor_id": vendor.vendor_id,
        "user_id": vendor.user_id,
        "bio": vendor.bio,
        "rating": vendor.rating,
        "num_events": vendor.num_events,
    }


def list_vendors(*, db: Session) -> list[dict]:
    """Return all vendors with basic user info joined in."""
    rows = db.query(Vendor, User).join(User, Vendor.user_id == User.user_id).all()
    return [
        {
            "vendor_id": v.vendor_id,
            "user_id": v.user_id,
            "bio": v.bio,
            "rating": v.rating,
            "num_events": v.num_events,
            "f_name": u.f_name,
            "l_name": u.l_name,
            "location": u.location,
            "pfp_url": u.pfp_url,
        }
        for v, u in rows
    ]


def search_vendors(
    *,
    service_name: str,
    latitude: float,
    longitude: float,
    db: Session,
) -> list[dict]:
    """Return vendors offering *service_name* within their travel radius
    of the given coordinates, sorted by distance (closest first).
    """
    results = (
        db.query(Vendor, Service, User)
        .join(Service, Vendor.vendor_id == Service.vendor_id)
        .join(User, Vendor.user_id == User.user_id)
        .filter(Service.name.ilike(f"%{service_name}%"))
        .all()
    )

    nearby_vendors: list[dict] = []

    for vendor, service, user in results:
        if user.latitude is None or user.longitude is None:
            continue

        distance_miles = calculate_distance_miles(
            latitude, longitude, user.latitude, user.longitude
        )

        if distance_miles <= vendor.travel_radius_miles:
            nearby_vendors.append(
                {
                    "vendor_id": vendor.vendor_id,
                    "user_id": user.user_id,
                    "first_name": user.f_name,
                    "last_name": user.l_name,
                    "service_name": service.name,
                    "service_price": service.price,
                    "distance_miles": round(distance_miles, 2),
                    "rating": vendor.rating,
                    "travel_radius_miles": vendor.travel_radius_miles,
                }
            )

    nearby_vendors.sort(key=lambda x: x["distance_miles"])
    return nearby_vendors
