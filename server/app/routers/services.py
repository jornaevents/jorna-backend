"""Thin router for service (offering) endpoints — delegates to service_service."""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User
from app.dependencies import get_current_user
from app.services.service_service import (
    ServiceError,
    create_service,
    delete_service,
    list_services,
    update_service,
)

router = APIRouter(prefix="/services", tags=["services"])


# ── Request schemas ───────────────────────────────────────────────────


class UpdateServiceRequest(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    duration_minutes: Optional[int] = None
    experience: Optional[str] = None
    media: Optional[list[str]] = None
    category: Optional[str] = None
    price_unit: Optional[str] = None
    description: Optional[str] = None


class CreateServiceRequest(BaseModel):
    name: str
    price: float
    duration_minutes: Optional[int] = None
    experience: str
    media: Optional[list[str]] = None
    category: Optional[str] = None
    price_unit: Optional[str] = None
    description: Optional[str] = None


# ── Routes ────────────────────────────────────────────────────────────


@router.post("", summary="Create a service")
def create_service_route(
    body: CreateServiceRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Add a new service offering. Only vendors can call this."""
    try:
        return create_service(
            user_id=current_user.user_id,
            name=body.name,
            price=body.price,
            duration_minutes=body.duration_minutes,
            experience=body.experience,
            media=body.media,
            category=body.category,
            price_unit=body.price_unit,
            description=body.description,
            db=db,
        )
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.patch("/{service_id}", summary="Update a service")
def update_service_route(
    service_id: str,
    body: UpdateServiceRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Partially update a service. Only the owning vendor may call this."""
    try:
        return update_service(
            user_id=current_user.user_id,
            service_id=service_id,
            update_data=body.model_dump(exclude_unset=True),
            db=db,
        )
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete("/{service_id}", summary="Delete a service", status_code=204)
def delete_service_route(
    service_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a service. Only the owning vendor may call this."""
    try:
        delete_service(user_id=current_user.user_id, service_id=service_id, db=db)
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("", summary="List services")
def list_services_route(
    vendor_id: Optional[str] = Query(None, description="Filter by vendor ID"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Return a list of services, optionally filtered by vendor_id. No auth required."""
    response = list_services(vendor_id=vendor_id, limit=limit, offset=offset, db=db)
    return response["items"]
