"""Business logic for vendor services (offerings)."""

from typing import Optional
from sqlalchemy.orm import Session

from app.db.models import Service, Vendor


class ServiceError(Exception):
    """Raised when a service operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _service_dict(service: Service) -> dict:
    return {
        "service_id": service.service_id,
        "vendor_id": service.vendor_id,
        "name": service.name,
        "price": service.price,
        "duration_minutes": service.duration_minutes,
        "experience": service.experience,
        "media": service.media,
        "category": service.category,
        "price_unit": service.price_unit,
        "description": service.description,
    }


def create_service(
    *,
    user_id: str,
    name: str,
    price: float,
    duration_minutes: Optional[int],
    experience: str,
    media: Optional[list[str]],
    category: Optional[str] = None,
    price_unit: Optional[str] = None,
    description: Optional[str] = None,
    db: Session,
) -> dict:
    """Create a service for the vendor linked to *user_id*. Raises 403 if not a vendor."""
    vendor = db.query(Vendor).filter(Vendor.user_id == user_id).first()
    if not vendor:
        raise ServiceError(403, "You must be a vendor to add services")
    service = Service(
        vendor_id=vendor.vendor_id,
        name=name,
        price=price,
        duration_minutes=duration_minutes,
        experience=experience,
        media=media,
        category=category,
        price_unit=price_unit,
        description=description,
    )
    db.add(service)
    db.commit()
    db.refresh(service)
    return _service_dict(service)


def list_services(
    *, vendor_id: Optional[str] = None, limit: int = 20, offset: int = 0, db: Session
) -> dict:
    """Return a paginated list of services, optionally filtered by *vendor_id*."""
    query = db.query(Service)
    if vendor_id:
        query = query.filter(Service.vendor_id == vendor_id)
    total = query.count()
    items = [_service_dict(s) for s in query.offset(offset).limit(limit).all()]
    return {"items": items, "total": total, "limit": limit, "offset": offset}
