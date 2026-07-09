"""Business logic for vendor services (offerings)."""

from typing import Optional
from sqlalchemy.orm import Session

from app.db.models import Service, Vendor, User
from app.services.storage_service import delete_service_image


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
        "subcategory": service.subcategory,
        "price_unit": service.price_unit,
        "description": service.description,
    }


def _service_with_vendor_dict(service: Service, vendor: Vendor, user: User) -> dict:
    """Service fields plus its vendor's display info — the shape the service-first
    swap picker needs (one call gives service + who offers it)."""
    d = _service_dict(service)
    d["vendor_name"] = f"{user.f_name} {user.l_name}".strip() if user else None
    d["vendor_rating"] = vendor.rating if vendor else None
    d["vendor_pfp_url"] = user.pfp_url if user else None
    return d


def create_service(
    *,
    user_id: str,
    name: str,
    price: float,
    duration_minutes: Optional[int],
    experience: str,
    media: Optional[list[str]],
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    price_unit: Optional[str] = None,
    description: Optional[str] = None,
    db: Session,
) -> dict:
    """Create a service for the vendor linked to *user_id*. Raises 403 if not a vendor.

    Service-first bundle matching keys off Service.category, so a service must
    always be categorized. When the client omits a category, fall back to the
    vendor's own category (and subcategory) so nothing is left uncategorized.
    """
    vendor = db.query(Vendor).filter(Vendor.user_id == user_id).first()
    if not vendor:
        raise ServiceError(403, "You must be a vendor to add services")
    if not category:
        category = vendor.category
        # Only inherit the vendor's subcategory alongside its category.
        if subcategory is None:
            subcategory = vendor.subcategory
    service = Service(
        vendor_id=vendor.vendor_id,
        name=name,
        price=price,
        duration_minutes=duration_minutes,
        experience=experience,
        media=media,
        category=category,
        subcategory=subcategory,
        price_unit=price_unit,
        description=description,
    )
    db.add(service)
    db.commit()
    db.refresh(service)
    return _service_dict(service)


def get_service(*, service_id: str, db: Session) -> dict:
    """Return a single service by ID. Raises 404 if not found."""
    service = db.query(Service).filter(Service.service_id == service_id).first()
    if not service:
        raise ServiceError(404, "Service not found")
    return _service_dict(service)


def list_services(
    *,
    vendor_id: Optional[str] = None,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    db: Session,
) -> dict:
    """Return a paginated list of services with their vendor info.

    Filterable by vendor_id (a vendor's own services) and/or category +
    subcategory (candidate services for a bundle slot, across all vendors —
    the service-first swap picker's list). Joins the vendor + user so each
    item includes vendor_name/rating/pfp in one call.
    """
    query = (
        db.query(Service, Vendor, User)
        .join(Vendor, Service.vendor_id == Vendor.vendor_id)
        .join(User, Vendor.user_id == User.user_id)
    )
    if vendor_id:
        query = query.filter(Service.vendor_id == vendor_id)
    if category:
        query = query.filter(Service.category == category)
    if subcategory:
        query = query.filter(Service.subcategory == subcategory)
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    items = [_service_with_vendor_dict(s, v, u) for s, v, u in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def update_service(*, user_id: str, service_id: str, update_data: dict, db: Session) -> dict:
    """Partially update a service. Raises 404 if not found, 403 if not the owner."""
    service = db.query(Service).filter(Service.service_id == service_id).first()
    if not service:
        raise ServiceError(404, "Service not found")
    vendor = db.query(Vendor).filter(Vendor.vendor_id == service.vendor_id).first()
    if not vendor or vendor.user_id != user_id:
        raise ServiceError(403, "Not authorized to edit this service")
    for field, value in update_data.items():
        setattr(service, field, value)
    db.commit()
    db.refresh(service)
    return _service_dict(service)


def add_service_image(*, user_id: str, service_id: str, image_url: str, db: Session) -> dict:
    """Append an already-uploaded image URL to the service's media list."""
    service = db.query(Service).filter(Service.service_id == service_id).first()
    if not service:
        raise ServiceError(404, "Service not found")
    vendor = db.query(Vendor).filter(Vendor.vendor_id == service.vendor_id).first()
    if not vendor or vendor.user_id != user_id:
        raise ServiceError(403, "Not authorized to edit this service")
    media = list(service.media or [])
    media.append(image_url)
    service.media = media
    db.commit()
    db.refresh(service)
    return _service_dict(service)


def remove_service_image(*, user_id: str, service_id: str, image_url: str, db: Session) -> dict:
    """Remove an image URL from the service's media list."""
    service = db.query(Service).filter(Service.service_id == service_id).first()
    if not service:
        raise ServiceError(404, "Service not found")
    vendor = db.query(Vendor).filter(Vendor.vendor_id == service.vendor_id).first()
    if not vendor or vendor.user_id != user_id:
        raise ServiceError(403, "Not authorized to edit this service")
    media = list(service.media or [])
    if image_url not in media:
        raise ServiceError(404, "Image not found on this service")
    media.remove(image_url)
    service.media = media
    db.commit()
    db.refresh(service)
    return _service_dict(service)


def delete_service(*, user_id: str, service_id: str, db: Session) -> None:
    """Delete a service. Raises 404 if not found, 403 if not the owner."""
    service = db.query(Service).filter(Service.service_id == service_id).first()
    if not service:
        raise ServiceError(404, "Service not found")
    vendor = db.query(Vendor).filter(Vendor.vendor_id == service.vendor_id).first()
    if not vendor or vendor.user_id != user_id:
        raise ServiceError(403, "Not authorized to delete this service")
    media = list(service.media or [])
    db.delete(service)
    db.commit()
    for url in media:
        delete_service_image(url)
