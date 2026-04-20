"""Thin router for vendor profile endpoints — delegates to vendor_service."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User, Vendor
from app.dependencies import get_current_user
from app.limiter import limiter
from app.models.schemas import VendorCategory
from app.services.vendor_service import (
    VendorError,
    create_vendor,
    get_vendor,
    get_my_vendor,
    update_vendor,
    list_vendors,
    search_vendors,
    add_tag_to_vendor,
    remove_tag_from_vendor,
    get_vendor_tags,
    list_all_tags,
)

router = APIRouter(prefix="/vendors", tags=["vendors"])


# ── Request schemas ───────────────────────────────────────────────────


class CreateVendorRequest(BaseModel):
    bio: str
    category: VendorCategory


class UpdateVendorRequest(BaseModel):
    bio: Optional[str] = None
    category: Optional[VendorCategory] = None
    travel_radius_miles: Optional[int] = None


class TagRequest(BaseModel):
    tag: str


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
            category=body.category.value,
            db=db,
        )
    except VendorError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("", summary="List all vendors")
def list_vendors_route(
    category: Optional[VendorCategory] = Query(None, description="Filter by vendor category"),
    tag: Optional[str] = Query(None, description="Filter by tag (e.g. 'bridal mehndi')"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Return a list of vendor profiles. Optionally filter by category and/or tag."""
    response = list_vendors(
        db=db,
        category=category.value if category else None,
        tag=tag,
        limit=limit,
        offset=offset,
    )
    return response["items"]


@router.get("/search", summary="Search vendors by service and location")
@limiter.limit("30/minute")
def vendor_search(
    request: Request,
    service_name: str,
    latitude: float,
    longitude: float,
    category: Optional[VendorCategory] = Query(None, description="Filter by vendor category"),
    tag: Optional[str] = Query(None, description="Filter by tag (e.g. 'bridal mehndi')"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Search for vendors offering a service within their travel radius, sorted by distance."""
    return search_vendors(
        service_name=service_name,
        latitude=latitude,
        longitude=longitude,
        category=category.value if category else None,
        tag=tag,
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


@router.patch("/me", summary="Update current user's vendor profile")
def update_my_vendor_route(
    body: UpdateVendorRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Partially update the authenticated user's vendor profile."""
    try:
        return update_vendor(
            user_id=current_user.user_id,
            update_data=body.model_dump(exclude_unset=True),
            db=db,
        )
    except VendorError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


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
