"""Checking in from the link in the reminder email.

A vendor standing in a hall with a van to unload is not going to find their
password. The link in their email has to work on its own, so the token in it is
the credential — the same shape as the RSVP links, and for the same reason.

What stops that being a problem is that the token isn't sufficient. Check-in is
GPS-gated: the caller has to prove they're within ~0.2 miles of the venue, and
that rule is the existing one — this hands off to booking_service.check_in
rather than reimplementing it, so the two can't drift apart. Somebody who steals
the email gets a link that refuses to do anything unless they drive to the
wedding.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import Vendor
from app.limiter import limiter
from app.services.booking_service import BookingError, check_in as svc_check_in
from app.services.reminder_service import ReminderError, booking_for_token, starts_at

router = APIRouter(tags=["check-in"])


class TokenCheckIn(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)


def _fail(e: ReminderError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/checkin/{token}", summary="What this check-in link is for")
@limiter.limit("60/minute")
def checkin_details(request: Request, token: str, db: Session = Depends(get_db)):
    """Enough to show a page worth trusting, and nothing more.

    A vendor should see which job this is before they press anything. What they
    should not see is the client's name, the price, or anything about the rest
    of the plan — the token came out of an inbox, and an inbox is not an
    account.
    """
    try:
        booking = booking_for_token(token, db)
    except ReminderError as e:
        raise _fail(e)

    start = starts_at(booking, db)
    return {
        "booking_id": booking.booking_id,
        "location": booking.location,
        "date_iso": booking.date_iso,
        "time_start": booking.time_start,
        "starts_at": start.isoformat() if start else None,
        "already_checked_in": bool(booking.vendor_checked_in_at),
    }


@router.post("/checkin/{token}", summary="Check in from the emailed link")
@limiter.limit("20/minute")
def checkin_with_token(
    request: Request,
    token: str,
    body: TokenCheckIn,
    db: Session = Depends(get_db),
):
    """Records the vendor's arrival, subject to the same GPS check as the app.

    The caller is taken to be the vendor on the booking — that's who the email
    went to, and the token names one booking. It's passed to check_in as that
    vendor's user, so every rule beyond this point is the one the in-app button
    already obeys, including what check-in means for escrow.
    """
    try:
        booking = booking_for_token(token, db)
    except ReminderError as e:
        raise _fail(e)

    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="That booking no longer has a vendor on it.")

    try:
        return svc_check_in(
            booking_id=booking.booking_id,
            caller_user_id=vendor.user_id,
            latitude=body.latitude,
            longitude=body.longitude,
            db=db,
        )
    except BookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
