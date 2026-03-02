"""Router for push-notification management (FCM token registration)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User

router = APIRouter(prefix="/notifications", tags=["notifications"])


class FCMTokenRegister(BaseModel):
    user_id: str
    fcm_token: str


class FCMTokenResponse(BaseModel):
    message: str
    user_id: str


@router.post("/register-token", summary="Register or update a user's FCM device token")
def register_fcm_token(body: FCMTokenRegister, db: Session = Depends(get_db)):
    """
    Called by the mobile app on launch (or when the FCM token refreshes)
    to associate a device token with a user.  This token is then used
    to deliver push notifications for booking events.
    """
    user = db.query(User).filter(User.user_id == body.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.fcm_token = body.fcm_token
    db.commit()

    return {"message": "FCM token registered successfully", "user_id": user.user_id}


@router.delete("/remove-token/{user_id}", summary="Remove a user's FCM token (opt-out)")
def remove_fcm_token(user_id: str, db: Session = Depends(get_db)):
    """
    Allows a user to opt out of push notifications by clearing their
    stored FCM token.
    """
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.fcm_token = None
    db.commit()

    return {"message": "FCM token removed successfully", "user_id": user.user_id}


@router.get("/token-status/{user_id}", summary="Check if a user has a registered FCM token")
def token_status(user_id: str, db: Session = Depends(get_db)):
    """Returns whether the user currently has an FCM token registered."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "user_id": user.user_id,
        "has_token": user.fcm_token is not None,
    }
