"""Router for group conversation endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.dependencies import get_current_user, user_from_token
from app.services.conversation_service import (
    ConversationError,
    list_conversations,
    get_conversation,
    send_group_message,
    get_group_messages,
    get_unread_count,
    _assert_is_member,
)
from app.services.ws_manager import manager

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
async def send_message_route(
    conversation_id: str,
    body: SendMessageRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a message to a group conversation, then push it to everyone currently
    connected to this conversation's WebSocket. Caller must be a member."""
    try:
        # send_group_message is sync (blocking DB) — run it off the event loop so
        # the broadcast below can await without stalling other sockets.
        message = await run_in_threadpool(
            send_group_message,
            conversation_id=conversation_id,
            content=body.content,
            caller_user_id=current_user.user_id,
            db=db,
        )
    except ConversationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    # Live-push to open sockets. Best-effort: a delivery failure must not fail the
    # send (the message is already persisted, and the 5s poll is the fallback).
    await manager.broadcast(conversation_id, message)
    return message


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


@router.websocket("/ws/{conversation_id}")
async def conversation_ws(
    websocket: WebSocket,
    conversation_id: str,
    db: Session = Depends(get_db),
):
    """Live message stream for a conversation. The client authenticates with its
    bearer token (Authorization header, or a `token` query param) and receives a
    JSON message object each time anyone posts to this conversation. The server
    only pushes — any client frames are ignored and just keep the socket open.
    """
    # Authenticate before accepting: prefer the Authorization header, fall back to
    # a query param (some WebSocket clients can't set headers).
    auth = websocket.headers.get("authorization")
    token = auth[7:] if auth and auth[:7].lower() == "bearer " else websocket.query_params.get("token")
    user = user_from_token(token, db)
    if not user:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        _assert_is_member(conversation_id, user.user_id, db)
    except ConversationError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    await manager.connect(conversation_id, websocket)
    try:
        while True:
            # We don't use inbound frames, but receiving keeps the connection
            # alive and surfaces the disconnect so we can clean up.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(conversation_id, websocket)
