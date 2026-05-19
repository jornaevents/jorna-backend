"""Router for event bundle endpoints."""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.dependencies import get_current_user
from app.services.bundle_service import (
    BundleError,
    create_bundle,
    get_bundle,
    get_bundle_conversations,
    list_bundles,
    add_booking_to_bundle,
    remove_booking_from_bundle,
    update_bundle_status,
    delete_bundle,
)

router = APIRouter(prefix="/bundles", tags=["bundles"])


class CreateBundleRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    event_id: Optional[str] = None
    booking_ids: list[str] = Field(default_factory=list)


class UpdateStatusRequest(BaseModel):
    status: str


@router.post("", summary="Create a bundle", status_code=201)
def create_bundle_route(
    body: CreateBundleRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new bundle, optionally linking an event and existing bookings."""
    try:
        return create_bundle(
            user_id=current_user.user_id,
            name=body.name,
            event_id=body.event_id,
            booking_ids=body.booking_ids,
            db=db,
        )
    except BundleError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("", summary="List current user's bundles")
def list_bundles_route(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return all bundles belonging to the authenticated user."""
    return list_bundles(user_id=current_user.user_id, db=db)


@router.get("/{bundle_id}", summary="Get a bundle with all its bookings")
def get_bundle_route(
    bundle_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return a bundle with all bookings, total cost, and status breakdown."""
    try:
        return get_bundle(bundle_id=bundle_id, caller_user_id=current_user.user_id, db=db)
    except BundleError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/{bundle_id}/conversations", summary="Get group conversations for a bundle")
def get_bundle_conversations_route(
    bundle_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return both group conversations (vendors_only and all_parties) for a bundle."""
    try:
        return get_bundle_conversations(bundle_id=bundle_id, caller_user_id=current_user.user_id, db=db)
    except BundleError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{bundle_id}/bookings/{booking_id}", summary="Add a booking to a bundle")
def add_booking_route(
    bundle_id: str,
    booking_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Add an existing booking to a bundle."""
    try:
        return add_booking_to_bundle(
            bundle_id=bundle_id, booking_id=booking_id,
            caller_user_id=current_user.user_id, db=db,
        )
    except BundleError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete("/{bundle_id}/bookings/{booking_id}", summary="Remove a booking from a bundle")
def remove_booking_route(
    bundle_id: str,
    booking_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a booking from a bundle without cancelling the booking."""
    try:
        return remove_booking_from_bundle(
            bundle_id=bundle_id, booking_id=booking_id,
            caller_user_id=current_user.user_id, db=db,
        )
    except BundleError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.patch("/{bundle_id}/status", summary="Update bundle status")
def update_status_route(
    bundle_id: str,
    body: UpdateStatusRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update bundle status: draft | active | completed | cancelled."""
    try:
        return update_bundle_status(
            bundle_id=bundle_id, status=body.status,
            caller_user_id=current_user.user_id, db=db,
        )
    except BundleError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete("/{bundle_id}", summary="Delete a bundle", status_code=204)
def delete_bundle_route(
    bundle_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a bundle. The individual bookings are kept but detached from the bundle."""
    try:
        delete_bundle(bundle_id=bundle_id, caller_user_id=current_user.user_id, db=db)
    except BundleError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
