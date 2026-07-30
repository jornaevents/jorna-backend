"""Business logic for vendor profiles, CRUD, and search / discovery."""

from sqlalchemy.orm import Session

from app.db.models import Vendor, Service, User, Tag, VendorAvailability, vendor_tags
from app.utils.location import calculate_distance_miles


def _normalize_tag(name: str) -> str:
    """Lowercase and strip whitespace so 'Bridal Mehndi' and 'bridal mehndi' are the same tag."""
    return name.strip().lower()


class VendorError(Exception):
    """Raised when a vendor operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def create_vendor(*, user_id: str, bio: str, category: str, subcategory: str | None = None, db: Session) -> dict:
    """Create a vendor profile for *user_id*. Raises 400 if one already exists."""
    existing = db.query(Vendor).filter(Vendor.user_id == user_id).first()
    if existing:
        raise VendorError(400, "You already have a vendor profile")
    vendor = Vendor(user_id=user_id, bio=bio, category=category, subcategory=subcategory, rating=0.0, num_events=0)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return {
        "vendor_id": vendor.vendor_id,
        "user_id": vendor.user_id,
        "bio": vendor.bio,
        "category": vendor.category,
        "subcategory": vendor.subcategory,
        "rating": vendor.rating,
        "num_events": vendor.num_events,
        "tags": [],
    }


def get_vendor(*, vendor_id: str, db: Session) -> dict:
    """Return a single vendor's full profile including tags. Raises 404 if not found."""
    row = (
        db.query(Vendor, User)
        .join(User, Vendor.user_id == User.user_id)
        .filter(Vendor.vendor_id == vendor_id)
        .first()
    )
    if not row:
        raise VendorError(404, "Vendor not found")
    v, u = row
    return {
        "vendor_id": v.vendor_id,
        "user_id": v.user_id,
        "bio": v.bio,
        "category": v.category,
        "subcategory": v.subcategory,
        "rating": v.rating,
        "num_events": v.num_events,
        "travel_radius_miles": v.travel_radius_miles,
        "open_to_long_distance": v.open_to_long_distance,
        "open_to_price_negotiation": v.open_to_price_negotiation,
        "open_to_location_negotiation": v.open_to_location_negotiation,
        "f_name": u.f_name,
        "l_name": u.l_name,
        "location": u.location,
        "pfp_url": u.pfp_url,
        # Public contact — surfaced on the storefront so clients can reach out.
        "email": u.email,
        "phone": u.phone,
        "tags": sorted(t.name for t in v.tags),
        "instagram_username": v.instagram_username,
        "instagram_tags": v.instagram_tags or [],
    }


def get_my_vendor(*, user_id: str, db: Session) -> dict:
    """Return the authenticated user's vendor profile. Raises 404 if no vendor exists."""
    row = (
        db.query(Vendor, User)
        .join(User, Vendor.user_id == User.user_id)
        .filter(Vendor.user_id == user_id)
        .first()
    )
    if not row:
        raise VendorError(404, "Vendor profile not found for this user")
    v, u = row
    return {
        "vendor_id": v.vendor_id,
        "user_id": v.user_id,
        "bio": v.bio,
        "category": v.category,
        "subcategory": v.subcategory,
        "rating": v.rating,
        "num_events": v.num_events,
        "travel_radius_miles": v.travel_radius_miles,
        "open_to_long_distance": v.open_to_long_distance,
        "open_to_price_negotiation": v.open_to_price_negotiation,
        "open_to_location_negotiation": v.open_to_location_negotiation,
        "f_name": u.f_name,
        "l_name": u.l_name,
        "location": u.location,
        "pfp_url": u.pfp_url,
        "tags": sorted(t.name for t in v.tags),
        "instagram_username": v.instagram_username,
        "instagram_tags": v.instagram_tags or [],
    }


def update_vendor(*, user_id: str, update_data: dict, db: Session) -> dict:
    """Apply *update_data* (partial) to the vendor profile and return the updated profile."""
    vendor = db.query(Vendor).filter(Vendor.user_id == user_id).first()
    if not vendor:
        raise VendorError(404, "Vendor profile not found for this user")
    
    # Validate category if provided
    if "category" in update_data:
        from app.models.schemas import VendorCategory
        try:
            VendorCategory(update_data["category"])
        except ValueError:
            raise VendorError(400, f"Invalid category: {update_data['category']}")

    # Validate subcategory against the (possibly updated) category
    if "subcategory" in update_data and update_data["subcategory"] is not None:
        from app.models.schemas import VENDOR_SUBCATEGORIES
        cat = update_data.get("category") or vendor.category
        valid = VENDOR_SUBCATEGORIES.get(cat, [])
        if valid and update_data["subcategory"] not in valid:
            raise VendorError(400, f"Invalid subcategory '{update_data['subcategory']}' for category '{cat}'. Valid: {valid}")
    
    # Validate travel_radius_miles if provided
    if "travel_radius_miles" in update_data:
        radius = update_data["travel_radius_miles"]
        if not isinstance(radius, int) or radius < 1 or radius > 500:
            raise VendorError(400, "Travel radius must be an integer between 1 and 500 miles")
    
    if "instagram_username" in update_data:
        ig = (update_data["instagram_username"] or "").strip().lstrip("@") or None
        # Check uniqueness if setting a new value
        if ig and ig != vendor.instagram_username:
            conflict = db.query(Vendor).filter(Vendor.instagram_username == ig).first()
            if conflict:
                raise VendorError(400, "That Instagram account is already linked to another vendor")
        update_data["instagram_username"] = ig

    for field, value in update_data.items():
        if field in ["bio", "category", "subcategory", "travel_radius_miles", "open_to_long_distance",
                     "open_to_price_negotiation", "open_to_location_negotiation", "instagram_username"]:
            setattr(vendor, field, value)
    
    db.commit()
    db.refresh(vendor)
    # Return updated vendor profile
    return get_my_vendor(user_id=user_id, db=db)


def list_vendors(
    *,
    db: Session,
    category: str | None = None,
    subcategory: str | None = None,
    tag: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Return a paginated list of vendors with basic user info joined in."""
    query = db.query(Vendor, User).join(User, Vendor.user_id == User.user_id)
    if category:
        query = query.filter(Vendor.category == category)
    if subcategory:
        query = query.filter(Vendor.subcategory == subcategory)
    if tag:
        normalized = _normalize_tag(tag)
        query = (
            query.join(vendor_tags, Vendor.vendor_id == vendor_tags.c.vendor_id)
                 .join(Tag, vendor_tags.c.tag_id == Tag.tag_id)
                 .filter(Tag.name == normalized)
        )
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    items = [
        {
            "vendor_id": v.vendor_id,
            "user_id": v.user_id,
            "bio": v.bio,
            "category": v.category,
            "subcategory": v.subcategory,
            "rating": v.rating,
            "num_events": v.num_events,
            "f_name": u.f_name,
            "l_name": u.l_name,
            "location": u.location,
            "pfp_url": u.pfp_url,
            "tags": [t.name for t in v.tags],
        }
        for v, u in rows
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def search_vendors(
    *,
    service_name: str = "",
    latitude: float | None = None,
    longitude: float | None = None,
    category: str | None = None,
    subcategory: str | None = None,
    state: str | None = None,
    tag: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    min_rating: float | None = None,
    sort_by: str = "distance",
    limit: int = 20,
    offset: int = 0,
    db: Session,
) -> dict:
    """Search vendors offering a service. When coordinates are given, results are
    filtered by distance and can sort by it — a traveling vendor against their own
    travel radius from where they're based, a venue against where the building
    actually stands. When they aren't (e.g. the swap picker only knows the event's
    state), the location filter falls back to a `state` match against the vendor's
    own location — coarse, and see the note there — and distance is omitted. `category`
    matches a vendor's category OR subcategory, so simplified keys like "dj" work
    alongside canonical ones like "music_entertainment".
    """
    from sqlalchemy import or_

    from app.utils.location import VENUE_MAX_DISTANCE_MILES

    query = (
        db.query(Vendor, Service, User)
        .join(Service, Vendor.vendor_id == Service.vendor_id)
        .join(User, Vendor.user_id == User.user_id)
    )
    if service_name:
        query = query.filter(Service.name.ilike(f"%{service_name}%"))
    if category:
        query = query.filter(or_(Vendor.category == category, Vendor.subcategory == category))
    if subcategory:
        # Service-first: narrow to the exact specialty so e.g. a "dj" slot lists
        # DJ services only, not every music_entertainment service (dhol included).
        query = query.filter(Service.subcategory == subcategory)
    # No coords → filter by the event's state string instead (best-effort).
    #
    # KNOWN GAP: for a venue this asks the wrong question — it matches the
    # owner's home state, and they may live nowhere near the building. Matching
    # Service.location instead was tried and reverted: it's a free-text street
    # address, so a two-letter code substring-matches inside ordinary words
    # ("MA" in "1 Market St"), trading a wrong-state venue for a wrong-state
    # match. Fixing it properly needs a structured state on the service rather
    # than a heuristic over an address. The coordinate path below is exact and
    # is what the app uses whenever a city is picked rather than free-typed.
    if state and state.strip() and state.strip().upper() != "TBD":
        query = query.filter(User.location.ilike(f"%{state.strip()}%"))
    if tag:
        normalized = _normalize_tag(tag)
        query = (
            query.join(vendor_tags, Vendor.vendor_id == vendor_tags.c.vendor_id)
                 .join(Tag, vendor_tags.c.tag_id == Tag.tag_id)
                 .filter(Tag.name == normalized)
        )
    if min_price is not None:
        query = query.filter(Service.price >= min_price)
    if max_price is not None:
        query = query.filter(Service.price <= max_price)
    if min_rating is not None:
        query = query.filter(Vendor.rating >= min_rating)

    results = query.all()
    have_coords = latitude is not None and longitude is not None

    nearby_vendors: list[dict] = []
    for vendor, service, user in results:
        distance_miles: float | None = None
        if have_coords:
            if service.category == "venue":
                # A venue is where the event happens, so it qualifies on where
                # the building stands — never on its owner's address or their
                # travel radius, and open_to_long_distance cannot waive it (a
                # building does not travel). Same rule as the bundle builder.
                # No coordinates means an unknowable distance, which is exactly
                # what this excludes; venue services created through the API
                # always carry them.
                if service.venue_latitude is None or service.venue_longitude is None:
                    continue
                distance_miles = calculate_distance_miles(
                    latitude, longitude, service.venue_latitude, service.venue_longitude
                )
                if distance_miles > VENUE_MAX_DISTANCE_MILES:
                    continue
            else:
                # Can't place a vendor with no coords relative to the event.
                if user.latitude is None or user.longitude is None:
                    continue
                distance_miles = calculate_distance_miles(
                    latitude, longitude, user.latitude, user.longitude
                )
                if not (vendor.open_to_long_distance or distance_miles <= vendor.travel_radius_miles):
                    continue
        nearby_vendors.append(
            {
                "vendor_id": vendor.vendor_id,
                "user_id": user.user_id,
                "first_name": user.f_name,
                "last_name": user.l_name,
                "category": vendor.category,
                # A result row is a vendor+service pair, so the row has to name
                # which service it is — without this a card can show a listing
                # but has no way to link to it.
                "service_id": service.service_id,
                "service_name": service.name,
                "service_price": service.price,
                "distance_miles": round(distance_miles, 2) if distance_miles is not None else None,
                # The vendor's blended rating across everything they sell. Kept
                # under this name because min_rating and sort_by=rating filter on
                # it, and a card falls back to it for an unreviewed listing.
                "rating": vendor.rating,
                # This listing's own record. A result row is a vendor+service
                # pair and the card leads with the service, so this is the number
                # that actually describes what's being shown.
                "service_rating": service.rating,
                "service_num_reviews": service.num_reviews,
                "location": user.location,
                "pfp_url": user.pfp_url,
                "travel_radius_miles": vendor.travel_radius_miles,
                "open_to_long_distance": vendor.open_to_long_distance,
                "tags": [t.name for t in vendor.tags],
            }
        )

    sort_keys = {
        "rating": lambda x: -(x["rating"] or 0),
        "price": lambda x: x["service_price"],
        "distance": lambda x: x["distance_miles"] if x["distance_miles"] is not None else float("inf"),
    }
    nearby_vendors.sort(key=sort_keys.get(sort_by, sort_keys["distance"]))

    total = len(nearby_vendors)
    return {
        "items": nearby_vendors[offset: offset + limit],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# ── Vendor deletion ───────────────────────────────────────────────────


def delete_vendor(*, user_id: str, db: Session) -> None:
    """Delete the vendor profile for *user_id*, including services and availability slots."""
    from sqlalchemy import delete as sql_delete
    vendor = db.query(Vendor).filter(Vendor.user_id == user_id).first()
    if not vendor:
        raise VendorError(404, "Vendor profile not found for this user")
    vendor_id = vendor.vendor_id
    db.execute(sql_delete(VendorAvailability).where(VendorAvailability.vendor_id == vendor_id))
    for service in db.query(Service).filter(Service.vendor_id == vendor_id).all():
        for url in list(service.media or []):
            from app.services.storage_service import delete_service_image
            delete_service_image(url)
        db.delete(service)
    db.execute(vendor_tags.delete().where(vendor_tags.c.vendor_id == vendor_id))
    db.delete(vendor)
    db.commit()


# ── Availability CRUD ─────────────────────────────────────────────────


def get_availability(*, vendor_id: str, db: Session) -> list[dict]:
    slots = db.query(VendorAvailability).filter(VendorAvailability.vendor_id == vendor_id).all()
    return [
        {
            "availability_id": s.availability_id,
            "day_of_week": s.day_of_week,
            "start_time": s.start_time,
            "end_time": s.end_time,
        }
        for s in slots
    ]


def set_availability(*, user_id: str, slots: list[dict], db: Session) -> list[dict]:
    """Replace all availability slots for the vendor with the provided list."""
    vendor = db.query(Vendor).filter(Vendor.user_id == user_id).first()
    if not vendor:
        raise VendorError(404, "Vendor profile not found for this user")
    db.query(VendorAvailability).filter(VendorAvailability.vendor_id == vendor.vendor_id).delete()
    for slot in slots:
        day = slot["day_of_week"]
        if not (0 <= day <= 6):
            raise VendorError(400, "day_of_week must be 0 (Monday) – 6 (Sunday)")
        db.add(VendorAvailability(
            vendor_id=vendor.vendor_id,
            day_of_week=day,
            start_time=slot["start_time"],
            end_time=slot["end_time"],
        ))
    db.commit()
    return get_availability(vendor_id=vendor.vendor_id, db=db)


# ── Tag management ────────────────────────────────────────────────────


def add_tag_to_vendor(*, vendor_id: str, tag_name: str, db: Session) -> dict:
    """Add a normalized tag to a vendor. Creates the tag row if it doesn't exist yet."""
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise VendorError(404, "Vendor not found")

    normalized = _normalize_tag(tag_name)
    if not normalized:
        raise VendorError(400, "Tag name cannot be empty")
    if len(normalized) > 100:
        raise VendorError(400, "Tag name must be 100 characters or fewer")

    tag = db.query(Tag).filter(Tag.name == normalized).first()
    if not tag:
        tag = Tag(name=normalized)
        db.add(tag)
        db.flush()  # assign tag_id without committing

    if len(vendor.tags) >= 20:
        raise VendorError(400, "Vendors may not have more than 20 tags")

    if tag not in vendor.tags:
        vendor.tags.append(tag)
        db.commit()

    return {"tag": tag.name}


def remove_tag_from_vendor(*, vendor_id: str, tag_name: str, db: Session) -> dict:
    """Remove a tag from a vendor's profile."""
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise VendorError(404, "Vendor not found")

    normalized = _normalize_tag(tag_name)
    tag = db.query(Tag).filter(Tag.name == normalized).first()
    if not tag or tag not in vendor.tags:
        raise VendorError(404, f"Tag '{normalized}' not found on this vendor")

    vendor.tags.remove(tag)
    db.commit()
    return {"message": f"Tag '{normalized}' removed"}


def get_vendor_tags(*, vendor_id: str, db: Session) -> list[str]:
    """Return all tag names for a given vendor, sorted alphabetically."""
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise VendorError(404, "Vendor not found")
    return sorted(t.name for t in vendor.tags)


def list_all_tags(*, db: Session) -> list[str]:
    """Return every tag name in the system, sorted alphabetically."""
    return [t.name for t in db.query(Tag).order_by(Tag.name).all()]
