"""The public inquiry form's endpoint (docs/DECISIONS.md #32).

No Depends(get_current_user): anyone with a vendor's link can send one, the
same trust model as /guest-bookings. The guards are in inquiry_service; the
per-IP limit is here.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.limiter import limiter
from app.services.inquiry_service import InquiryError, send_inquiry

router = APIRouter(prefix="/vendors", tags=["inquiries"])


class InquiryRequest(BaseModel):
    name: str = Field(..., max_length=255)
    email: str = Field(..., max_length=255)
    phone: Optional[str] = Field(default=None, max_length=50)
    event_date_iso: Optional[str] = Field(default=None, max_length=50)
    event_type: Optional[str] = Field(default=None, max_length=50)
    guest_count: Optional[int] = None
    note: Optional[str] = Field(default=None, max_length=2000)
    # The honeypot: hidden on the form, so only a bot fills it in.
    website: Optional[str] = None


@router.post("/{vendor_id}/inquiries", summary="Send a vendor an inquiry, no account needed", status_code=201)
@limiter.limit("5/minute")
def send_inquiry_route(
    request: Request,
    vendor_id: str,
    body: InquiryRequest,
    db: Session = Depends(get_db),
):
    """Lands in the vendor's Leads as a new lead; emails the vendor and sends
    the person who asked a short confirmation. Replies only
    {received, vendor_name}."""
    try:
        return send_inquiry(vendor_id=vendor_id, **body.model_dump(), db=db)
    except InquiryError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
