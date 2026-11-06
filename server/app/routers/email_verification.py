"""Proving an account's email with a code (docs/DECISIONS.md #44)."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User
from app.dependencies import get_current_user
from app.limiter import limiter
from app.services.email_verification_service import (
    EmailVerificationError,
    send_code,
    verify_code,
)

router = APIRouter(prefix="/auth/email", tags=["auth"])


class VerifyEmailRequest(BaseModel):
    code: str = Field(..., max_length=20)


@router.post("/send-code", summary="Email the signed-in account a code to confirm its address")
@limiter.limit("3/minute")
def send_code_route(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """{email_verified: true} if there's nothing to do; otherwise
    {email_verified: false, sent_to, expires_in_minutes}."""
    try:
        return send_code(db, current_user)
    except EmailVerificationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/verify", summary="Confirm the signed-in account's email with its code")
@limiter.limit("10/minute")
def verify_route(
    request: Request,
    body: VerifyEmailRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return verify_code(db, current_user, body.code)
    except EmailVerificationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
