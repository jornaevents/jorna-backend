"""Thin router for vendor profile endpoints — delegates to vendor_service."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User
from app.dependencies import get_current_user
from app.models.schemas import VendorCategory
from app.services.vendor_service import VendorError, create_vendor, list_vendors

router = APIRouter(prefix="/vendors", tags=["vendors"])


# ── Request schemas ───────────────────────────────────────────────────


class CreateVendorRequest(BaseModel):
    bio: str
    category: VendorCategory


# ── Routes ────────────────────────────────────────────────────────────


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
    db: Session = Depends(get_db),
):
    """Return all vendor profiles with basic user info. Optionally filter by category."""
    return list_vendors(db=db, category=category.value if category else None)

