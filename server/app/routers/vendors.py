"""Thin router for vendor profile endpoints — delegates to vendor_service."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User, Vendor, Service
from app.dependencies import get_current_user, get_current_admin
from app.services.service_service import media_url
from app.limiter import limiter
from app.models.schemas import (
    CATEGORY_LABELS,
    SUBCATEGORY_LABELS,
    VendorCategory,
    VENDOR_SUBCATEGORIES,
)
from app.services.vendor_service import (
    VendorError,
    create_vendor,
    get_vendor,
    get_my_vendor,
    update_vendor,
    delete_vendor,
    list_vendors,
    search_vendors,
    get_availability,
    set_availability,
    add_tag_to_vendor,
    remove_tag_from_vendor,
    get_vendor_tags,
    list_all_tags,
)

router = APIRouter(prefix="/vendors", tags=["vendors"])


# ── Request schemas ───────────────────────────────────────────────────


class VendorSpecializationItem(BaseModel):
    """One entry in the `specializations` list — a category, optionally
    narrowed to a subcategory within it. Same shape and validation as the
    top-level category/subcategory pair below, just repeatable."""

    category: VendorCategory
    subcategory: Optional[str] = None

    @field_validator("subcategory")
    @classmethod
    def validate_subcategory(cls, v, info):
        if v is None:
            return v
        category = info.data.get("category")
        cat_key = category.value if isinstance(category, VendorCategory) else category
        valid = VENDOR_SUBCATEGORIES.get(cat_key, [])
        if valid and v not in valid:
            raise ValueError(f"Invalid subcategory '{v}' for category '{cat_key}'. Valid: {valid}")
        return v


class CreateVendorRequest(BaseModel):
    bio: str
    # Optional: what a vendor sells is decided per service, and every service
    # carries its own category. Asking again here made the signup form look like
    # it was categorising a service — and forced a single answer out of anyone
    # who does two things. Left unset, the vendor is "other" until their first
    # service says otherwise, which search reads through (see search_vendors).
    category: Optional[VendorCategory] = None
    subcategory: Optional[str] = None
    # The full list a vendor picks during onboarding; category/subcategory
    # above mirror its first entry, since that's what search still filters on.
    specializations: Optional[list[VendorSpecializationItem]] = None

    @field_validator("subcategory")
    @classmethod
    def validate_subcategory(cls, v, info):
        if v is None:
            return v
        category = info.data.get("category")
        cat_key = category.value if isinstance(category, VendorCategory) else category
        valid = VENDOR_SUBCATEGORIES.get(cat_key, [])
        if valid and v not in valid:
            raise ValueError(f"Invalid subcategory '{v}' for category '{cat_key}'. Valid: {valid}")
        return v

    @field_validator("specializations")
    @classmethod
    def validate_specializations(cls, v):
        if v is not None and len(v) > 20:
            raise ValueError("No more than 20 specializations")
        return v


class UpdateVendorRequest(BaseModel):
    bio: Optional[str] = None
    category: Optional[VendorCategory] = None
    subcategory: Optional[str] = None
    specializations: Optional[list[VendorSpecializationItem]] = None
    travel_radius_miles: Optional[int] = None
    open_to_long_distance: Optional[bool] = None
    open_to_price_negotiation: Optional[bool] = None
    open_to_location_negotiation: Optional[bool] = None
    instagram_username: Optional[str] = None
    payment_method: Optional[str] = None
    venmo_handle: Optional[str] = None
    zelle_contact: Optional[str] = None
    default_deposit_percent: Optional[int] = None
    default_cancellation_window_hours: Optional[int] = None
    default_overtime_rate_cents: Optional[int] = None
    default_addon_rate_cents: Optional[int] = None
    default_contract_terms: Optional[dict] = None
    default_guest_count_mode: Optional[str] = None

    @field_validator("specializations")
    @classmethod
    def validate_specializations(cls, v):
        if v is not None and len(v) > 20:
            raise ValueError("No more than 20 specializations")
        return v


class InstagramEnrichRequest(BaseModel):
    tags: list[str] = []
    images: list[str] = []
    bio: Optional[str] = None


class TagRequest(BaseModel):
    tag: str


class AvailabilitySlot(BaseModel):
    day_of_week: int
    start_time: str
    end_time: str


class SetAvailabilityRequest(BaseModel):
    slots: list[AvailabilitySlot]


# ── Routes ────────────────────────────────────────────────────────────
# IMPORTANT: static paths must come before parameterised paths so
# FastAPI doesn't match e.g. "search", "me", or "tags" as a {vendor_id}.


@router.post("", summary="Create a vendor profile")
def create_vendor_route(
    body: CreateVendorRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Promote the current user to a vendor by creating a vendor profile."""
    try:
        return create_vendor(
            user_id=current_user.user_id,
            bio=body.bio,
            category=body.category.value if body.category else None,
            subcategory=body.subcategory,
            specializations=(
                [{"category": s.category.value, "subcategory": s.subcategory} for s in body.specializations]
                if body.specializations is not None
                else None
            ),
            db=db,
        )
    except VendorError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("", summary="List all vendors")
def list_vendors_route(
    category: Optional[VendorCategory] = Query(None, description="Filter by vendor category"),
    subcategory: Optional[str] = Query(None, description="Filter by subcategory (e.g. 'dj', 'mehndi_artist')"),
    tag: Optional[str] = Query(None, description="Filter by tag (e.g. 'bridal mehndi')"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Return a list of vendor profiles. Optionally filter by category, subcategory, and/or tag."""
    response = list_vendors(
        db=db,
        category=category.value if category else None,
        subcategory=subcategory,
        tag=tag,
        limit=limit,
        offset=offset,
    )
    return response


@router.get("/categories", summary="The vendor category taxonomy")
def vendor_categories_route():
    """Return every vendor category with its display name and subcategories.

    The taxonomy is validated server-side (an invalid category/subcategory pair
    is rejected on create), so clients need the authoritative list to build a
    vendor form. Serving it here keeps each client from hardcoding its own copy
    and drifting out of sync. Public — it's reference data, not user data.

    Declared before `/{vendor_id}` so "categories" isn't read as a vendor id.
    """
    return {
        "categories": [
            {
                "value": category.value,
                "label": CATEGORY_LABELS.get(category.value, category.value),
                "subcategories": [
                    {"value": sub, "label": SUBCATEGORY_LABELS.get(sub, sub)}
                    for sub in VENDOR_SUBCATEGORIES.get(category.value, [])
                ],
            }
            for category in VendorCategory
        ]
    }


@router.get("/search", summary="Search vendors by service and location")
@limiter.limit("30/minute")
def vendor_search(
    request: Request,
    service_name: str = "",
    latitude: Optional[float] = Query(None, description="Event latitude (for distance/travel-radius filtering)"),
    longitude: Optional[float] = Query(None, description="Event longitude"),
    category: Optional[str] = Query(None, description="Category or subcategory key (e.g. 'venue', 'dj')"),
    subcategory: Optional[str] = Query(None, description="Narrow to a service subcategory (e.g. 'dj', 'mehndi_artist') so a slot lists only that specialty"),
    state: Optional[str] = Query(None, description="Event state/region, used to filter when coords aren't given"),
    tag: Optional[str] = Query(None, description="Filter by tag (e.g. 'bridal mehndi')"),
    min_price: Optional[float] = Query(None, ge=0, description="Minimum service price"),
    max_price: Optional[float] = Query(None, ge=0, description="Maximum service price"),
    min_rating: Optional[float] = Query(None, ge=0, le=5, description="Minimum vendor rating (0–5)"),
    sort_by: Optional[str] = Query("distance", description="Sort order: distance, rating, price"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Search for vendors offering a service. With coordinates, results are
    filtered to each vendor's travel radius and can sort by distance; without
    them, it falls back to a `state` match (used by the bundle swap picker).
    `category` matches a vendor's category or subcategory."""
    return search_vendors(
        service_name=service_name,
        latitude=latitude,
        longitude=longitude,
        category=category,
        subcategory=subcategory,
        state=state,
        tag=tag,
        min_price=min_price,
        max_price=max_price,
        min_rating=min_rating,
        sort_by=sort_by or "distance",
        limit=limit,
        offset=offset,
        db=db,
    )


@router.get("/me", summary="Get current user's vendor profile")
def get_my_vendor_route(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the authenticated user's vendor profile."""
    from app.services.vendor_service import get_my_vendor
    try:
        return get_my_vendor(user_id=current_user.user_id, db=db)
    except VendorError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/me/clients", summary="Clients CRM: the authenticated vendor's own bookings, grouped by client")
def get_my_clients_route(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services.contract_service import ContractError, get_vendor_clients

    vendor = db.query(Vendor).filter(Vendor.user_id == current_user.user_id).first()
    if not vendor:
        raise HTTPException(status_code=403, detail="You must be a vendor to view this")
    try:
        return get_vendor_clients(vendor_id=vendor.vendor_id, caller_user_id=current_user.user_id, db=db)
    except ContractError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete("/me", summary="Delete current user's vendor profile", status_code=204)
def delete_my_vendor_route(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Permanently delete the authenticated user's vendor profile, services, and availability."""
    try:
        delete_vendor(user_id=current_user.user_id, db=db)
    except VendorError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.patch("/me", summary="Update current user's vendor profile")
def update_my_vendor_route(
    body: UpdateVendorRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Partially update the authenticated user's vendor profile."""
    # specializations is handled separately: model_dump() would otherwise leave
    # its enum members un-normalized (fine in memory, not what a JSON column
    # should store), so it's excluded from the generic dump and rebuilt as
    # plain dicts the same way create_vendor_route already does.
    update_data = body.model_dump(exclude_unset=True, exclude={"specializations"})
    if body.specializations is not None:
        update_data["specializations"] = [
            {"category": s.category.value, "subcategory": s.subcategory} for s in body.specializations
        ]
    try:
        return update_vendor(
            user_id=current_user.user_id,
            update_data=update_data,
            db=db,
        )
    except VendorError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/me/availability", summary="Get current vendor's availability slots")
def get_my_availability(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the authenticated vendor's weekly availability slots."""
    try:
        vendor = db.query(Vendor).filter(Vendor.user_id == current_user.user_id).first()
        if not vendor:
            raise HTTPException(status_code=404, detail="Vendor profile not found")
        return get_availability(vendor_id=vendor.vendor_id, db=db)
    except VendorError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.put("/me/availability", summary="Replace current vendor's availability slots")
def set_my_availability(
    body: SetAvailabilityRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Replace all availability slots. Send an empty list to clear all slots.
    day_of_week: 0=Monday, 6=Sunday. Times are strings like '09:00'."""
    try:
        return set_availability(
            user_id=current_user.user_id,
            slots=[s.model_dump() for s in body.slots],
            db=db,
        )
    except VendorError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/instagram-linked", summary="List vendors with Instagram linked (admin only)")
def list_instagram_linked(
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Return all vendors that have an Instagram username set.
    Used by the scraper to know which accounts to enrich."""
    rows = (
        db.query(Vendor, User)
        .join(User, Vendor.user_id == User.user_id)
        .filter(Vendor.instagram_username.isnot(None))
        .all()
    )
    return [
        {
            "vendor_id": v.vendor_id,
            "instagram_username": v.instagram_username,
            "category": v.category,
            "f_name": u.f_name,
            "l_name": u.l_name,
        }
        for v, u in rows
    ]


@router.post("/{vendor_id}/instagram-enrich", summary="Post Instagram-scraped data back to a vendor (admin only)")
def instagram_enrich(
    vendor_id: str,
    body: InstagramEnrichRequest,
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Called by the scraper after scraping a vendor's Instagram.
    Updates instagram_tags (separate from user-inputted tags) and
    optionally adds scraped images to the vendor's first service."""
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    # Store Instagram-generated tags separately from user-inputted tags
    if body.tags:
        vendor.instagram_tags = body.tags[:20]

    # Optionally update bio only if vendor hasn't set one
    if body.bio and (not vendor.bio or vendor.bio.strip() == ""):
        vendor.bio = body.bio[:500]

    db.commit()

    # Add scraped images to the vendor's first service if they have one
    if body.images:
        service = db.query(Service).filter(Service.vendor_id == vendor_id).first()
        if service:
            existing = list(service.media or [])
            # Typed the same as every other write path — see the identical
            # fix (and its rationale) in admin.py's run_scraper.
            existing_urls = {media_url(m) for m in existing}
            new_images = [
                {"url": img, "type": "image", "thumbnail_url": None}
                for img in body.images
                if img not in existing_urls
            ]
            service.media = (existing + new_images)[:9]
            db.commit()

    return {
        "vendor_id": vendor_id,
        "instagram_tags": vendor.instagram_tags or [],
        "message": f"Enriched with {len(body.tags)} tags and {len(body.images)} images.",
    }


@router.get("/tags", summary="List all tags")
def list_tags_route(db: Session = Depends(get_db)):
    """Return every tag in the system sorted alphabetically. Useful for autocomplete."""
    return {"tags": list_all_tags(db=db)}


@router.get("/{vendor_id}", summary="Get a single vendor profile")
def get_vendor_route(vendor_id: str, db: Session = Depends(get_db)):
    """Return the full profile for one vendor, including their tags."""
    try:
        return get_vendor(vendor_id=vendor_id, db=db)
    except VendorError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/{vendor_id}/tags", summary="Get a vendor's tags")
def get_vendor_tags_route(vendor_id: str, db: Session = Depends(get_db)):
    """Return all tags on a vendor's profile."""
    try:
        return {"tags": get_vendor_tags(vendor_id=vendor_id, db=db)}
    except VendorError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{vendor_id}/tags", summary="Add a tag to a vendor")
def add_tag_route(
    vendor_id: str,
    body: TagRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Add a tag to a vendor's profile. Only the vendor's own account may do this."""
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    if vendor.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this vendor")
    try:
        return add_tag_to_vendor(vendor_id=vendor_id, tag_name=body.tag, db=db)
    except VendorError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete("/{vendor_id}/tags/{tag_name}", summary="Remove a tag from a vendor")
def remove_tag_route(
    vendor_id: str,
    tag_name: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a tag from a vendor's profile. Only the vendor's own account may do this."""
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    if vendor.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this vendor")
    try:
        return remove_tag_from_vendor(vendor_id=vendor_id, tag_name=tag_name, db=db)
    except VendorError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
