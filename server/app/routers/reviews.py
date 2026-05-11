"""Router for reviews and vendor ratings."""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.dependencies import get_current_user
from app.services.review_service import (
    ReviewError,
    create_review,
    get_vendor_reviews,
    get_booking_review,
)

router = APIRouter(prefix="/reviews", tags=["reviews"])


class CreateReviewRequest(BaseModel):
    booking_id: str
    rating: float
    comment: Optional[str] = None


@router.post("", summary="Submit a review for a completed booking", status_code=201)
def create_review_route(
    body: CreateReviewRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Leave a review for a booking. One review per booking, clients only.
    Automatically updates the vendor's rating and event count."""
    try:
        return create_review(
            booking_id=body.booking_id,
            rating=body.rating,
            comment=body.comment,
            caller_user_id=current_user.user_id,
            db=db,
        )
    except ReviewError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/vendor/{vendor_id}", summary="Get reviews for a vendor")
def get_vendor_reviews_route(
    vendor_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Return paginated reviews for a vendor. No auth required."""
    return get_vendor_reviews(vendor_id=vendor_id, limit=limit, offset=offset, db=db)


@router.get("/booking/{booking_id}", summary="Get the review for a specific booking")
def get_booking_review_route(
    booking_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the review for a booking. Caller must be the client or vendor."""
    try:
        return get_booking_review(
            booking_id=booking_id, caller_user_id=current_user.user_id, db=db
        )
    except ReviewError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
