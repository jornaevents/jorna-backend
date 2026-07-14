"""Business logic for user events."""

from typing import Optional
from sqlalchemy.orm import Session

from app.db.models import Event


class EventError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _event_dict(event: Event) -> dict:
    return {
        "event_id": event.event_id,
        "user_id": event.user_id,
        "name": event.name,
        "date_iso": event.date_iso,
        "location": event.location,
        "event_type": event.event_type,
        "description": event.description,
        "guest_count": event.guest_count,
        "budget": event.budget,
        "services_needed": event.services_needed,
        "venue_latitude": event.venue_latitude,
        "venue_longitude": event.venue_longitude,
    }


def create_event(
    *,
    user_id: str,
    name: str,
    date_iso: str,
    location: str,
    event_type: Optional[str] = None,
    description: Optional[str] = None,
    guest_count: Optional[int] = None,
    budget: Optional[float] = None,
    services_needed: Optional[list[str]] = None,
    db: Session,
) -> dict:
    event = Event(
        user_id=user_id,
        name=name,
        date_iso=date_iso,
        location=location,
        event_type=event_type,
        description=description,
        guest_count=guest_count,
        budget=budget,
        services_needed=services_needed,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return _event_dict(event)


def update_event(*, user_id: str, event_id: str, update_data: dict, db: Session) -> dict:
    event = db.query(Event).filter(Event.event_id == event_id).first()
    if not event:
        raise EventError(404, "Event not found")
    if event.user_id != user_id:
        raise EventError(403, "Not authorized to edit this event")
    for field, value in update_data.items():
        setattr(event, field, value)
    db.commit()
    db.refresh(event)
    return _event_dict(event)


def list_events(*, user_id: str, db: Session) -> list[dict]:
    events = db.query(Event).filter(Event.user_id == user_id).all()
    return [_event_dict(e) for e in events]


def delete_event(*, user_id: str, event_id: str, db: Session) -> None:
    event = db.query(Event).filter(Event.event_id == event_id).first()
    if not event:
        raise EventError(404, "Event not found")
    if event.user_id != user_id:
        raise EventError(403, "Not authorized to delete this event")
    db.delete(event)
    db.commit()
