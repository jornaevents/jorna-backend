"""Router for group conversation endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.dependencies import get_current_user
from app.services.conversation_service import (
    ConversationError,
    list_conversations,
    get_conversation,
    send_group_message,
    get_group_messages,
    get_unread_count,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])


class SendMessageRequest(BaseModel):
    content: str


@router.get("", summary="List all conversations the current user is in")
def list_conversations_route(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return all group conversations the authenticated user is a member of,
    ordered by most recently created."""
    return list_conversations(caller_user_id=current_user.user_id, db=db)


@router.get("/unread-count", summary="Get total unread group message count")
def unread_count_route(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return total unread group messages across all conversations."""
    return get_unread_count(caller_user_id=current_user.user_id, db=db)


@router.get("/{conversation_id}", summary="Get a conversation and its members")
def get_conversation_route(
    conversation_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return conversation details and member list. Caller must be a member."""
    try:
        return get_conversation(
            conversation_id=conversation_id,
            caller_user_id=current_user.user_id,
            db=db,
        )
    except ConversationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{conversation_id}/messages", summary="Send a message to a group conversation", status_code=201)
def send_message_route(
    conversation_id: str,
    body: SendMessageRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a message to a group conversation. Caller must be a member."""
    try:
        return send_group_message(
            conversation_id=conversation_id,
            content=body.content,
            caller_user_id=current_user.user_id,
            db=db,
        )
    except ConversationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/{conversation_id}/messages", summary="Get messages in a group conversation")
def get_messages_route(
    conversation_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return paginated messages oldest first. Automatically marks messages as read."""
    try:
        return get_group_messages(
            conversation_id=conversation_id,
            caller_user_id=current_user.user_id,
            limit=limit,
            offset=offset,
            db=db,
        )
    except ConversationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
