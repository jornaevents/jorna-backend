"""Router for booking-scoped messaging."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.dependencies import get_current_user, get_current_verified_user
from app.services.message_service import (
    MessageError,
    send_message,
    get_conversation,
    mark_read,
    get_unread_count,
)

router = APIRouter(prefix="/messages", tags=["messages"])


class SendMessageRequest(BaseModel):
    booking_id: str
    content: str


@router.post("", summary="Send a message on a booking", status_code=201)
def send_message_route(
    body: SendMessageRequest,
    current_user=Depends(get_current_verified_user),
    db: Session = Depends(get_db),
):
    """Send a message to the other party on a booking. Caller must be the client or vendor."""
    try:
        return send_message(
            booking_id=body.booking_id,
            content=body.content,
            caller_user_id=current_user.user_id,
            db=db,
        )
    except MessageError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/booking/{booking_id}", summary="Get the conversation for a booking")
def get_conversation_route(
    booking_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return all messages for a booking, oldest first.
    Automatically marks received messages as read."""
    try:
        return get_conversation(
            booking_id=booking_id,
            caller_user_id=current_user.user_id,
            limit=limit,
            offset=offset,
            db=db,
        )
    except MessageError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.patch("/{message_id}/read", summary="Mark a message as read")
def mark_read_route(
    message_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark a single received message as read."""
    try:
        return mark_read(
            message_id=message_id,
            caller_user_id=current_user.user_id,
            db=db,
        )
    except MessageError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/unread-count", summary="Get total unread message count for the current user")
def unread_count_route(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the number of unread messages across all conversations."""
    return get_unread_count(caller_user_id=current_user.user_id, db=db)
