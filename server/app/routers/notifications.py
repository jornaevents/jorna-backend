"""Thin router for push-notification token management — delegates to notification_service."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.dependencies import get_current_user
from app.services.notification_service import (
    NotificationServiceError,
    register_fcm_token as svc_register_fcm_token,
    remove_fcm_token as svc_remove_fcm_token,
    get_token_status as svc_get_token_status,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


class FCMTokenRegister(BaseModel):
    fcm_token: str


@router.post("/register-token", summary="Register or update a user's FCM device token")
def register_fcm_token(
    body: FCMTokenRegister,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Called by the mobile app on launch to associate a device token with a user."""
    try:
        return svc_register_fcm_token(
            user_id=current_user.user_id, fcm_token=body.fcm_token, db=db
        )
    except NotificationServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete("/remove-token/{user_id}", summary="Remove a user's FCM token (opt-out)")
def remove_fcm_token(
    user_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Allows a user to opt out of push notifications. Users can only remove their own token."""
    if current_user.user_id != user_id:
        raise HTTPException(status_code=403, detail="You can only remove your own FCM token")
    try:
        return svc_remove_fcm_token(user_id=user_id, db=db)
    except NotificationServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/token-status/{user_id}", summary="Check if a user has a registered FCM token")
def token_status(
    user_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Returns whether the user currently has an FCM token registered. Users can only check their own."""
    if current_user.user_id != user_id:
        raise HTTPException(status_code=403, detail="You can only check your own token status")
    try:
        return svc_get_token_status(user_id=user_id, db=db)
    except NotificationServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
