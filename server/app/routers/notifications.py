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
    # Which kind of device this token is for. Defaults to "ios" so the existing
    # mobile client works unchanged; the web client sends "web".
    platform: str = "ios"


@router.post("/register-token", summary="Register or update a device's push token")
def register_fcm_token(
    body: FCMTokenRegister,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Called by a client on launch to associate a device token with a user.
    One user can have several (a phone plus one or more browsers)."""
    try:
        return svc_register_fcm_token(
            user_id=current_user.user_id,
            fcm_token=body.fcm_token,
            platform=body.platform,
            db=db,
        )
    except NotificationServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete("/remove-token/{user_id}", summary="Remove a user's push token(s) (opt-out)")
def remove_fcm_token(
    user_id: str,
    token: str | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Opt out of push. Pass ?token= to remove a single device (e.g. this browser
    on sign-out); omit it to remove all of the user's devices. Users can only
    remove their own."""
    if current_user.user_id != user_id:
        raise HTTPException(status_code=403, detail="You can only remove your own push tokens")
    try:
        return svc_remove_fcm_token(user_id=user_id, token=token, db=db)
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
