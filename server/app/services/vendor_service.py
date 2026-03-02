"""Business logic for vendor search / discovery."""

from sqlalchemy.orm import Session

from app.db.models import Vendor, Service, User
from app.utils.location import calculate_distance_miles


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
