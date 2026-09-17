"""Business logic for vendor profiles, CRUD, and search / discovery."""

from sqlalchemy.orm import Session

from app.config import ESCROW_ENABLED
from app.db.models import Vendor, Service, User, Tag, VendorAvailability, vendor_tags
from app.utils.location import calculate_distance_miles


def _normalize_tag(name: str) -> str:
    """Lowercase and strip whitespace so 'Bridal Mehndi' and 'bridal mehndi' are the same tag."""
    return name.strip().lower()


def _service_cover_url(media) -> str | None:
    """The photo a search card should lead with: this listing's own first image,
    or a video's poster frame if that's all it has. Older rows predate the
    {"url", "type", "thumbnail_url"} shape and store a bare URL string — treated
    as an image, same as routers/services.py's own defensive read of this column.
    """
    if not media:
        return None
    for item in media:
        kind = item.get("type") if isinstance(item, dict) else "image"
        if kind == "image":
            return item.get("url") if isinstance(item, dict) else item
    for item in media:
        if isinstance(item, dict) and item.get("type") == "video" and item.get("thumbnail_url"):
            return item["thumbnail_url"]
    return None


class VendorError(Exception):
    """Raised when a vendor operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def create_vendor(
    *,
    user_id: str,
    bio: str,
    category: str | None = None,
    subcategory: str | None = None,
    specializations: list[dict] | None = None,
    db: Session,
) -> dict:
    """Create a vendor profile for *user_id*. Raises 400 if one already exists.

    ``category`` is optional. The column is NOT NULL, so an unset one becomes
    "other" — a placeholder, not a claim. What this vendor actually sells is
    each service's own category, and search reads those; see search_vendors.

    ``specializations`` is the full category(+subcategory) list; ``category``/
    ``subcategory`` above still get set independently (mirroring its first
    entry, from the router) since search and the older list endpoints filter
    on those columns, not this one.
    """
    existing = db.query(Vendor).filter(Vendor.user_id == user_id).first()
    if existing:
        raise VendorError(400, "You already have a vendor profile")
    vendor = Vendor(
        user_id=user_id,
        bio=bio,
        category=category or "other",
        subcategory=subcategory,
        specializations=specializations,
        rating=0.0,
        num_events=0,
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return {
        "vendor_id": vendor.vendor_id,
        "user_id": vendor.user_id,
        "bio": vendor.bio,
        "category": vendor.category,
        "subcategory": vendor.subcategory,
        "specializations": vendor.specializations or [],
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
        "specializations": v.specializations or [],
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
        # "stripe" (protected, escrow-held) or "manual" (paid directly via
        # Venmo/Zelle) — a client needs this before booking, so it's public.
        # venmo_handle/zelle_contact stay private until an actual booking
        # exists (see bundle_service._booking_summary).
        "payment_method": v.payment_method,
        # Only meaningful when payment_method is "stripe": a client can still
        # send this vendor a request while it's False, but checkout will
        # refuse the charge until Stripe Connect onboarding finishes (see
        # stripe_service's stripe_onboarding_complete checks) — surfaced here
        # so that failure doesn't land only at the payment step, after a
        # client has already gotten the vendor to approve a request.
        "stripe_ready": v.stripe_onboarding_complete,
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
        "specializations": v.specializations or [],
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
        "payment_method": v.payment_method,
        "venmo_handle": v.venmo_handle,
        "zelle_contact": v.zelle_contact,
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

    if "payment_method" in update_data and update_data["payment_method"] not in ("stripe", "manual"):
        raise VendorError(400, "payment_method must be 'stripe' or 'manual'")

    if "venmo_handle" in update_data:
        update_data["venmo_handle"] = (update_data["venmo_handle"] or "").strip() or None

    if "zelle_contact" in update_data:
        update_data["zelle_contact"] = (update_data["zelle_contact"] or "").strip() or None

    # Escrow disabled → manual is the only payment track, and it needs
    # somewhere for a client to actually send money. Only enforced when this
    # update actually touches a payment field — an unrelated save (e.g. bio
    # during an earlier onboarding step, before payment info is ever set)
    # must not be blocked by a requirement it isn't trying to satisfy yet.
    # Checked against the resulting state, not just this request's fields, so
    # e.g. clearing venmo_handle with no zelle_contact on file still errors.
    # See docs/DECISIONS.md #12.
    touches_payment_fields = bool({"payment_method", "venmo_handle", "zelle_contact"} & update_data.keys())
    if not ESCROW_ENABLED and touches_payment_fields:
        if update_data.get("payment_method", "manual") != "manual":
            raise VendorError(400, "payment_method must be 'manual' — Stripe escrow is currently disabled")
        resulting_venmo = update_data.get("venmo_handle", vendor.venmo_handle)
        resulting_zelle = update_data.get("zelle_contact", vendor.zelle_contact)
        if not resulting_venmo and not resulting_zelle:
            raise VendorError(400, "Add a Venmo handle or Zelle contact so clients can pay you")

    for field, value in update_data.items():
        if field in ["bio", "category", "subcategory", "specializations", "travel_radius_miles",
                     "open_to_long_distance", "open_to_price_negotiation",
                     "open_to_location_negotiation", "instagram_username",
                     "payment_method", "venmo_handle", "zelle_contact"]:
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
    from sqlalchemy import or_

    query = db.query(Vendor, User).join(User, Vendor.user_id == User.user_id)
    # A vendor is in a category if they say so or if they sell something in it.
    # The second half is what keeps them findable now that signup no longer asks
    # — an unset category is stored as "other", which is a placeholder rather
    # than an answer.
    #
    # EXISTS rather than a join to Service: this returns vendors, and joining
    # would repeat one per matching service, inflating `total` and tearing holes
    # in the pagination below.
    if category:
        query = query.filter(
            or_(
                Vendor.category == category,
                db.query(Service)
                .filter(Service.vendor_id == Vendor.vendor_id, Service.category == category)
                .exists(),
            )
        )
    if subcategory:
        query = query.filter(
            or_(
                Vendor.subcategory == subcategory,
                db.query(Service)
                .filter(
                    Service.vendor_id == Vendor.vendor_id,
                    Service.subcategory == subcategory,
                )
                .exists(),
            )
        )
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
            "payment_method": v.payment_method,
            "stripe_ready": v.stripe_onboarding_complete,
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
    from sqlalchemy import and_, or_

    from app.utils.location import VENUE_MAX_DISTANCE_MILES

    query = (
        db.query(Vendor, Service, User)
        .join(Service, Vendor.vendor_id == Service.vendor_id)
        .join(User, Vendor.user_id == User.user_id)
    )
    if service_name:
        query = query.filter(Service.name.ilike(f"%{service_name}%"))
    if category:
        # Service-first, matching what this endpoint returns: a row here is one
        # vendor paired with one of their listings, so the question is what that
        # listing is — not what its owner mostly does. Filtering on the vendor
        # put a DJ's lighting service in the results for "dj", and left a vendor
        # who sells across two categories findable under only one of them.
        #
        # The vendor's own category is still consulted, but only for a service
        # that has none of its own: Service.category is nullable, and rows
        # predating service-level categorisation would otherwise vanish.
        query = query.filter(
            or_(
                Service.category == category,
                Service.subcategory == category,
                and_(
                    Service.category.is_(None),
                    or_(Vendor.category == category, Vendor.subcategory == category),
                ),
            )
        )
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
                # This listing's own photo, not the vendor's avatar — a card
                # leads with the service (see the comment above on why category
                # filtering is service-first too), so it should show what's
                # being sold, not who's selling it. Falls back to pfp_url only
                # when the service itself has no photo yet.
                "service_photo_url": _service_cover_url(service.media),
                "travel_radius_miles": vendor.travel_radius_miles,
                "open_to_long_distance": vendor.open_to_long_distance,
                "tags": [t.name for t in vendor.tags],
                "payment_method": vendor.payment_method,
                "stripe_ready": vendor.stripe_onboarding_complete,
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
