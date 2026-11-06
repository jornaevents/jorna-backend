"""The inquiry form's endpoint (docs/DECISIONS.md #32, #45).

Signed in: a client with a confirmed email sends it, and it opens their
thread with the vendor. The guards are in inquiry_service; the per-IP limit
is here.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User
from app.dependencies import get_current_user
from app.limiter import limiter
from app.services.inquiry_service import InquiryError, send_inquiry
from app.services.ws_manager import manager

router = APIRouter(prefix="/vendors", tags=["inquiries"])


class InquiryRequest(BaseModel):
    # Older pages still send name and email; the account's are used instead.
    name: Optional[str] = Field(default=None, max_length=255)
    email: Optional[str] = Field(default=None, max_length=255)
    phone: Optional[str] = Field(default=None, max_length=50)
    event_date_iso: Optional[str] = Field(default=None, max_length=50)
    event_type: Optional[str] = Field(default=None, max_length=50)
    guest_count: Optional[int] = None
    note: Optional[str] = Field(default=None, max_length=2000)
    # The honeypot: hidden on the form, so only a bot fills it in.
    website: Optional[str] = None


@router.post("/{vendor_id}/inquiries", summary="Send a vendor an inquiry", status_code=201)
@limiter.limit("5/minute")
async def send_inquiry_route(
    request: Request,
    vendor_id: str,
    body: InquiryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Opens or reuses the client's thread with the vendor, posts the inquiry
    there, and makes (or refreshes) their lead. Emails the vendor and sends
    the client a copy. Replies {received, vendor_name, conversation_id}."""
    try:
        result = await run_in_threadpool(
            send_inquiry,
            vendor_id=vendor_id,
            caller=current_user,
            **body.model_dump(exclude={"name", "email"}),
            db=db,
        )
    except InquiryError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    message = result.pop("message", None)
    if message is not None:
        await manager.broadcast(result["conversation_id"], message)
    return result
