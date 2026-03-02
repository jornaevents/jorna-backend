"""Thin router for service (offering) endpoints — delegates to service_service."""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User
from app.dependencies import get_current_user
from app.services.service_service import ServiceError, create_service, list_services

router = APIRouter(prefix="/services", tags=["services"])


# ── Request schemas ───────────────────────────────────────────────────


class CreateServiceRequest(BaseModel):
    name: str
    price: float
    duration_minutes: int
    experience: str
    media: Optional[list[str]] = None


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
            db=db,
        )
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("", summary="List services")
def list_services_route(
    vendor_id: Optional[str] = Query(None, description="Filter by vendor ID"),
    db: Session = Depends(get_db),
):
    """Return all services, optionally filtered by vendor_id. No auth required."""
    return list_services(vendor_id=vendor_id, db=db)
