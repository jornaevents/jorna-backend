"""Thin router for user profile endpoints — delegates to user_service."""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User
from app.dependencies import get_current_user
from app.services.user_service import UserError, get_user, update_user

router = APIRouter(tags=["users"])


# ── Request schemas ───────────────────────────────────────────────────


class UpdateMeRequest(BaseModel):
    f_name: Optional[str] = None
    l_name: Optional[str] = None
    phone: Optional[str] = None
    age: Optional[int] = None
    location: Optional[str] = None
    gender: Optional[str] = None
    language: Optional[str] = None
    pfp_url: Optional[str] = None


# ── Routes ────────────────────────────────────────────────────────────


@router.get("/me", summary="Get current user profile")
def get_me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return the authenticated user's profile."""
    try:
        return get_user(user_id=current_user.user_id, db=db)
    except UserError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.patch("/me", summary="Update current user profile")
def update_me(
    body: UpdateMeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Partially update the authenticated user's profile."""
    try:
        return update_user(
            user_id=current_user.user_id,
            update_data=body.model_dump(exclude_unset=True),
            db=db,
        )
    except UserError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
