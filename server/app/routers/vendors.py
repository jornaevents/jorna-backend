"""Thin router for vendor profile endpoints — delegates to vendor_service."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User
from app.dependencies import get_current_user
from app.services.vendor_service import VendorError, create_vendor, list_vendors

router = APIRouter(prefix="/vendors", tags=["vendors"])


# ── Request schemas ───────────────────────────────────────────────────


class CreateVendorRequest(BaseModel):
    bio: str


# ── Routes ────────────────────────────────────────────────────────────


@router.post("", summary="Create a vendor profile")
def create_vendor_route(
    body: CreateVendorRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Promote the current user to a vendor by creating a vendor profile."""
    try:
        return create_vendor(user_id=current_user.user_id, bio=body.bio, db=db)
    except VendorError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("", summary="List all vendors")
def list_vendors_route(db: Session = Depends(get_db)):
    """Return all vendor profiles with basic user info. No auth required."""
    return list_vendors(db=db)
