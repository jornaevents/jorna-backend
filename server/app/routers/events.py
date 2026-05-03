"""Thin router for event endpoints — delegates to event_service."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User
from app.dependencies import get_current_user
from app.services.event_service import EventError, create_event, update_event, list_events

router = APIRouter(prefix="/events", tags=["events"])


# ── Request schemas ───────────────────────────────────────────────────


class CreateEventRequest(BaseModel):
    name: str
    date_iso: str
    location: str
    event_type: Optional[str] = None
    description: Optional[str] = None
    guest_count: Optional[int] = None
    budget: Optional[float] = None
    services_needed: Optional[list[str]] = None


class UpdateEventRequest(BaseModel):
    name: Optional[str] = None
    date_iso: Optional[str] = None
    location: Optional[str] = None
    event_type: Optional[str] = None
    description: Optional[str] = None
    guest_count: Optional[int] = None
    budget: Optional[float] = None
    services_needed: Optional[list[str]] = None


# ── Routes ────────────────────────────────────────────────────────────


@router.post("", summary="Create an event", status_code=201)
def create_event_route(
    body: CreateEventRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new event owned by the authenticated user."""
    try:
        return create_event(
            user_id=current_user.user_id,
            name=body.name,
            date_iso=body.date_iso,
            location=body.location,
            event_type=body.event_type,
            description=body.description,
            guest_count=body.guest_count,
            budget=body.budget,
            services_needed=body.services_needed,
            db=db,
        )
    except EventError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("", summary="List current user's events")
def list_events_route(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return all events belonging to the authenticated user."""
    return list_events(user_id=current_user.user_id, db=db)


@router.patch("/{event_id}", summary="Update an event")
def update_event_route(
    event_id: str,
    body: UpdateEventRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Partially update an event. Only the event owner may call this."""
    try:
        return update_event(
            user_id=current_user.user_id,
            event_id=event_id,
            update_data=body.model_dump(exclude_unset=True),
            db=db,
        )
    except EventError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
