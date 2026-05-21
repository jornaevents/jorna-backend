"""Business logic for reviews and vendor ratings."""

from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.db.models import Booking, Review, Vendor, User


class ReviewError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _review_dict(review: Review, reviewer: User | None = None) -> dict:
    return {
        "review_id": review.review_id,
        "booking_id": review.booking_id,
        "vendor_id": review.vendor_id,
        "user_id": review.user_id,
        "rating": review.rating,
        "comment": review.comment,
        "created_at": review.created_at.isoformat(),
        "reviewer_name": f"{reviewer.f_name} {reviewer.l_name}" if reviewer else None,
        "reviewer_pfp": reviewer.pfp_url if reviewer else None,
    }


def _recalculate_vendor_rating(vendor: Vendor, db: Session) -> None:
    """Recompute vendor rating and num_events from all reviews."""
    reviews = db.query(Review).filter(Review.vendor_id == vendor.vendor_id).all()
    vendor.num_events = len(reviews)
    vendor.rating = round(sum(r.rating for r in reviews) / len(reviews), 2) if reviews else 0.0
    db.commit()


def create_review(
    *,
    booking_id: str,
    rating: float,
    comment: str | None,
    caller_user_id: str,
    db: Session,
) -> dict:
    """Submit a review for a completed booking. One review per booking, clients only."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise ReviewError(404, "Booking not found")
    if booking.user_id != caller_user_id:
        raise ReviewError(403, "Only the client of a booking can leave a review")
    if booking.status not in ("approved", "payment_confirmed"):
        raise ReviewError(400, "Reviews can only be left for approved or completed bookings")
    if not (1 <= rating <= 5):
        raise ReviewError(400, "Rating must be between 1 and 5")

    existing = db.query(Review).filter(Review.booking_id == booking_id).first()
    if existing:
        raise ReviewError(400, "You have already reviewed this booking")

    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    if not vendor:
        raise ReviewError(404, "Vendor not found")

    review = Review(
        booking_id=booking_id,
        vendor_id=booking.vendor_id,
        user_id=caller_user_id,
        rating=rating,
        comment=comment,
        created_at=datetime.now(timezone.utc),
    )
    db.add(review)
    db.flush()

    _recalculate_vendor_rating(vendor, db)

    reviewer = db.query(User).filter(User.user_id == caller_user_id).first()
    return _review_dict(review, reviewer)


def get_vendor_reviews(
    *, vendor_id: str, limit: int = 20, offset: int = 0, db: Session
) -> dict:
    """Return paginated reviews for a vendor, newest first."""
    query = db.query(Review).filter(Review.vendor_id == vendor_id).order_by(Review.created_at.desc())
    total = query.count()
    reviews = query.offset(offset).limit(limit).all()
    user_ids = {r.user_id for r in reviews}
    users = {u.user_id: u for u in db.query(User).filter(User.user_id.in_(user_ids)).all()}
    return {
        "items": [_review_dict(r, users.get(r.user_id)) for r in reviews],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def delete_review(*, review_id: str, caller_user_id: str, is_admin: bool, db: Session) -> dict:
    """Delete a review. The author or an admin may delete."""
    review = db.query(Review).filter(Review.review_id == review_id).first()
    if not review:
        raise ReviewError(404, "Review not found")
    if not is_admin and review.user_id != caller_user_id:
        raise ReviewError(403, "You can only delete your own reviews")

    vendor = db.query(Vendor).filter(Vendor.vendor_id == review.vendor_id).first()
    db.delete(review)
    db.flush()

    if vendor:
        _recalculate_vendor_rating(vendor, db)

    return {"message": "Review deleted", "review_id": review_id}


def get_booking_review(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    """Return the review for a specific booking. Caller must be the client or vendor."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise ReviewError(404, "Booking not found")

    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    is_vendor = vendor is not None and vendor.user_id == caller_user_id
    if booking.user_id != caller_user_id and not is_vendor:
        raise ReviewError(403, "Not authorized to view this review")

    review = db.query(Review).filter(Review.booking_id == booking_id).first()
    if not review:
        raise ReviewError(404, "No review found for this booking")

    reviewer = db.query(User).filter(User.user_id == review.user_id).first()
    return _review_dict(review, reviewer)
