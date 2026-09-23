"""Business logic for vendor services (offerings)."""

from typing import Optional
from sqlalchemy.orm import Session

from app.db.models import Booking, Service, Vendor, User
from app.services.storage_service import delete_service_image, delete_service_video


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
        "negotiable": service.negotiable,
        # This listing's own record, not its vendor's. `vendor_rating` below is
        # still the vendor's, and the two answer different questions — how good
        # this thing is, and how good the person behind it is.
        "rating": service.rating,
        "num_reviews": service.num_reviews,
        "location": service.location,
        "venue_latitude": service.venue_latitude,
        "venue_longitude": service.venue_longitude,
        "require_guest_count": service.require_guest_count,
        "require_performer_count": service.require_performer_count,
        "status": service.status or "active",
        "included_hours": service.included_hours,
        "inclusions": service.inclusions or [],
        "add_ons": service.add_ons or [],
        "deposit_percent": service.deposit_percent,
        "cancellation_window_hours": service.cancellation_window_hours,
        "overtime_rate_cents": service.overtime_rate_cents,
        "sort_order": service.sort_order,
    }


# The one definition of "a client can see and book this", for every query
# that lists packages to clients (search, bundles, the public list). Hidden
# packages are the vendor's private ones (still usable in their contracts);
# archived ones are retired. Lookups by id for an *existing* booking must not
# use this — an archived package's bookings still need their row.
def listed(query):
    return query.filter(Service.status == "active")


def format_experience(years: Optional[int]) -> str:
    """Service.experience is required free text that predates
    Vendor.years_experience; older clients still read it."""
    if years is None:
        return ""
    return "1 year" if years == 1 else f"{years} years"


def _require_venue_location(
    category: Optional[str],
    location: Optional[str],
    venue_latitude: Optional[float],
    venue_longitude: Optional[float],
) -> None:
    """Venue services must carry a location with map coordinates — a booked venue
    anchors the event's venue and supplies the coords traveling vendors check in
    against. No-op for every other category."""
    if category == "venue" and (
        not location or venue_latitude is None or venue_longitude is None
    ):
        raise ServiceError(400, "Venue services require a location with map coordinates.")


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
    experience: Optional[str],
    media: Optional[list[str]],
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    price_unit: Optional[str] = None,
    description: Optional[str] = None,
    negotiable: bool = False,
    location: Optional[str] = None,
    venue_latitude: Optional[float] = None,
    venue_longitude: Optional[float] = None,
    require_guest_count: bool = False,
    require_performer_count: bool = False,
    status: str = "active",
    included_hours: Optional[float] = None,
    inclusions: Optional[list[str]] = None,
    add_ons: Optional[list[dict]] = None,
    deposit_percent: Optional[int] = None,
    cancellation_window_hours: Optional[int] = None,
    overtime_rate_cents: Optional[int] = None,
    sort_order: Optional[int] = None,
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
    _require_venue_location(category, location, venue_latitude, venue_longitude)
    service = Service(
        vendor_id=vendor.vendor_id,
        name=name,
        price=price,
        duration_minutes=duration_minutes,
        # Optional now that years in business live on the vendor; still
        # written for clients that read the old field.
        experience=experience if experience else format_experience(vendor.years_experience),
        media=media,
        category=category,
        subcategory=subcategory,
        price_unit=price_unit,
        description=description,
        negotiable=negotiable,
        location=location,
        venue_latitude=venue_latitude,
        venue_longitude=venue_longitude,
        require_guest_count=require_guest_count,
        require_performer_count=require_performer_count,
        status=status,
        included_hours=included_hours,
        inclusions=inclusions,
        add_ons=add_ons,
        deposit_percent=deposit_percent,
        cancellation_window_hours=cancellation_window_hours,
        overtime_rate_cents=overtime_rate_cents,
        sort_order=sort_order,
    )
    db.add(service)
    db.commit()
    db.refresh(service)
    return _service_dict(service)


def get_service(*, service_id: str, db: Session) -> dict:
    """Return a single service by ID, with its vendor's display info.

    The same shape the list endpoint returns, so a caller that has a service
    from either route reads the same keys. The service detail page needs the
    vendor's name and photo to say who is offering the thing, and fetching that
    separately would mean a second round trip for two strings that live one join
    away.

    The join is left outer: a service whose vendor row is missing is a broken
    listing, but a 500 on the page is worse than a card with no name on it.
    """
    row = (
        db.query(Service, Vendor, User)
        .outerjoin(Vendor, Service.vendor_id == Vendor.vendor_id)
        .outerjoin(User, Vendor.user_id == User.user_id)
        .filter(Service.service_id == service_id)
        .first()
    )
    if not row:
        raise ServiceError(404, "Service not found")
    service, vendor, user = row
    return _service_with_vendor_dict(service, vendor, user)


def list_services(
    *,
    vendor_id: Optional[str] = None,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    include_unlisted: bool = False,
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
    # include_unlisted is only ever passed for the vendor's own list (the
    # router checks ownership); everyone else sees what's bookable.
    if not include_unlisted:
        query = listed(query)
    total = query.count()
    # The vendor's own order first; unordered packages after, by name, so
    # the list is stable between requests.
    rows = (
        query.order_by(Service.sort_order.is_(None), Service.sort_order, Service.name)
        .offset(offset)
        .limit(limit)
        .all()
    )
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
    # Enforce the venue-location rule on the merged result (the update may have
    # switched the category to "venue" or cleared a venue's location). Validate
    # before mutating so a rejected update leaves the row untouched.
    def _merged(field):
        return update_data[field] if field in update_data else getattr(service, field)
    _require_venue_location(
        _merged("category"), _merged("location"),
        _merged("venue_latitude"), _merged("venue_longitude"),
    )
    # status is NOT NULL: an explicit null means "no change", not "clear".
    if update_data.get("status", "unset") is None:
        update_data = {k: v for k, v in update_data.items() if k != "status"}
    for field, value in update_data.items():
        setattr(service, field, value)
    db.commit()
    db.refresh(service)
    return _service_dict(service)


def media_url(entry) -> Optional[str]:
    """A media entry's URL, whichever shape it's in.

    Rows written before typed media existed still hold bare strings; a
    migration backfills them to {"url", "type", "thumbnail_url"} on deploy,
    but reading defensively here means a row that somehow slips through
    still matches by URL instead of silently never being found. Public (not
    `_media_url`) because the Instagram-enrichment scraper (admin.py) needs
    the same defensive read when deduping against a service's existing media —
    it's what wrote un-typed rows *after* the migration ran, in the first
    place (see 0056_rebackfill_service_media.py).
    """
    if isinstance(entry, dict):
        return entry.get("url")
    return entry


def _delete_media_entry(entry) -> None:
    """Remove one media entry's backing files from storage — the video/photo
    itself, plus a video's separately-stored thumbnail."""
    if isinstance(entry, dict):
        if entry.get("type") == "video":
            delete_service_video(entry.get("url", ""))
            if entry.get("thumbnail_url"):
                delete_service_image(entry["thumbnail_url"])
        else:
            delete_service_image(entry.get("url", ""))
    else:
        delete_service_image(entry)


def _owning_vendor(service: Service, user_id: str, db: Session) -> Vendor:
    vendor = db.query(Vendor).filter(Vendor.vendor_id == service.vendor_id).first()
    if not vendor or vendor.user_id != user_id:
        raise ServiceError(403, "Not authorized to edit this service")
    return vendor


def add_service_image(*, user_id: str, service_id: str, image_url: str, db: Session) -> dict:
    """Append an already-uploaded image URL to the service's media list."""
    service = db.query(Service).filter(Service.service_id == service_id).first()
    if not service:
        raise ServiceError(404, "Service not found")
    _owning_vendor(service, user_id, db)
    media = list(service.media or [])
    media.append({"url": image_url, "type": "image", "thumbnail_url": None})
    service.media = media
    db.commit()
    db.refresh(service)
    return _service_dict(service)


def _remove_service_media(*, user_id: str, service_id: str, url: str, db: Session) -> tuple[dict, object]:
    service = db.query(Service).filter(Service.service_id == service_id).first()
    if not service:
        raise ServiceError(404, "Service not found")
    _owning_vendor(service, user_id, db)
    media = list(service.media or [])
    match = next((m for m in media if media_url(m) == url), None)
    if match is None:
        raise ServiceError(404, "Media item not found on this service")
    media.remove(match)
    service.media = media
    db.commit()
    db.refresh(service)
    return _service_dict(service), match


def remove_service_image(*, user_id: str, service_id: str, image_url: str, db: Session) -> dict:
    """Remove a photo URL from the service's media list."""
    result, _ = _remove_service_media(user_id=user_id, service_id=service_id, url=image_url, db=db)
    return result


def remove_service_video(*, user_id: str, service_id: str, video_url: str, db: Session) -> tuple[dict, Optional[str]]:
    """Remove a video from the service's media list.

    Returns (updated service dict, thumbnail_url) — the router deletes both
    the video and its thumbnail from storage, the same way it already deletes
    a removed photo, so DB writes and storage cleanup stay in one place.
    """
    result, match = _remove_service_media(user_id=user_id, service_id=service_id, url=video_url, db=db)
    thumbnail_url = match.get("thumbnail_url") if isinstance(match, dict) else None
    return result, thumbnail_url


def delete_service(*, user_id: str, service_id: str, db: Session) -> bool:
    """Delete a service. Raises 404 if not found, 403 if not the owner.

    A package any booking still points at is archived instead: the booking's
    service_id is a non-null foreign key, so deleting it used to fail (500 on
    Postgres), and the client would have lost the record of what it booked
    anyway. Returns True if it was archived rather than deleted. Either way
    it's gone from every list a client sees, so callers treat both the same.
    """
    service = db.query(Service).filter(Service.service_id == service_id).first()
    if not service:
        raise ServiceError(404, "Service not found")
    _owning_vendor(service, user_id, db)
    if db.query(Booking.booking_id).filter(Booking.service_id == service_id).first():
        service.status = "archived"
        db.commit()
        return True
    media = list(service.media or [])
    db.delete(service)
    db.commit()
    for entry in media:
        _delete_media_entry(entry)
    return False
