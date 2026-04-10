"""Thin router for vendor profile endpoints — delegates to vendor_service."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User, Vendor
from app.dependencies import get_current_user
from app.models.schemas import VendorCategory
from app.services.vendor_service import (
    VendorError,
    create_vendor,
    get_vendor,
    list_vendors,
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


class TagRequest(BaseModel):
    tag: str


# ── Routes ────────────────────────────────────────────────────────────
# IMPORTANT: static paths must be declared before parameterised paths so
# FastAPI doesn't match e.g. "tags" or "search" as a {vendor_id} value.


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
    db: Session = Depends(get_db),
):
    """Return all vendor profiles with basic user info. Optionally filter by category and/or tag."""
    return list_vendors(db=db, category=category.value if category else None, tag=tag)


# Static paths — must come before /{vendor_id} ────────────────────────

@router.get("/tags", summary="List all tags")
def list_tags_route(db: Session = Depends(get_db)):
    """Return every tag in the system sorted alphabetically. Useful for autocomplete."""
    return {"tags": list_all_tags(db=db)}


# Parameterised single-vendor path ────────────────────────────────────

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
