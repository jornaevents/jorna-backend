"""Business logic for reviews and vendor ratings."""

from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.db.models import Booking, Review, Service, Vendor, User


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
        "service_id": review.service_id,
        "user_id": review.user_id,
        "rating": review.rating,
        "comment": review.comment,
        "created_at": review.created_at.isoformat(),
        "reviewer_name": f"{reviewer.f_name} {reviewer.l_name}" if reviewer else None,
        "reviewer_pfp": reviewer.pfp_url if reviewer else None,
    }


def _mean(ratings: list[float]) -> float:
    return round(sum(ratings) / len(ratings), 2) if ratings else 0.0


def _recalculate_vendor_rating(vendor: Vendor, db: Session) -> None:
    """Recompute vendor rating and num_events from all reviews."""
    reviews = db.query(Review).filter(Review.vendor_id == vendor.vendor_id).all()
    vendor.num_events = len(reviews)
    vendor.rating = _mean([r.rating for r in reviews])
    db.commit()


def _recalculate_service_rating(service_id: str | None, db: Session) -> None:
    """Recompute one listing's rating from the reviews naming it.

    Recomputed from scratch rather than adjusted, so a rating can't drift away
    from the reviews a client can read underneath it — the number and the list
    are always the same arithmetic.

    A review written before 0040 whose service is gone carries no service_id;
    it still counts for the vendor and there is nothing here to update.
    """
    if service_id is None:
        return
    service = db.query(Service).filter(Service.service_id == service_id).first()
    if not service:
        return
    ratings = [
        r.rating for r in db.query(Review).filter(Review.service_id == service_id).all()
    ]
    service.num_reviews = len(ratings)
    service.rating = _mean(ratings)
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
        service_id=booking.service_id,
        user_id=caller_user_id,
        rating=rating,
        comment=comment,
        created_at=datetime.now(timezone.utc),
    )
    db.add(review)
    db.flush()

    _recalculate_vendor_rating(vendor, db)
    _recalculate_service_rating(booking.service_id, db)

    reviewer = db.query(User).filter(User.user_id == caller_user_id).first()
    return _review_dict(review, reviewer)


def _paginated_reviews(query, *, limit: int, offset: int, db: Session) -> dict:
    """Newest first, with each reviewer's name and photo resolved in one query."""
    query = query.order_by(Review.created_at.desc())
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


def get_vendor_reviews(
    *, vendor_id: str, limit: int = 20, offset: int = 0, db: Session
) -> dict:
    """Return paginated reviews for a vendor — everything they've been reviewed
    on, across all their listings."""
    return _paginated_reviews(
        db.query(Review).filter(Review.vendor_id == vendor_id),
        limit=limit,
        offset=offset,
        db=db,
    )


def get_service_reviews(
    *, service_id: str, limit: int = 20, offset: int = 0, db: Session
) -> dict:
    """Return paginated reviews for one listing.

    Narrower than the vendor's by design: this is what a client reading a
    service page is actually asking, and answering it with the vendor's whole
    record lends a new listing a reputation earned somewhere else.
    """
    return _paginated_reviews(
        db.query(Review).filter(Review.service_id == service_id),
        limit=limit,
        offset=offset,
        db=db,
    )


def delete_review(*, review_id: str, caller_user_id: str, is_admin: bool, db: Session) -> dict:
    """Delete a review. The author or an admin may delete."""
    review = db.query(Review).filter(Review.review_id == review_id).first()
    if not review:
        raise ReviewError(404, "Review not found")
    if not is_admin and review.user_id != caller_user_id:
        raise ReviewError(403, "You can only delete your own reviews")

    vendor = db.query(Vendor).filter(Vendor.vendor_id == review.vendor_id).first()
    # Read before the delete — afterwards the row is gone and with it the only
    # record of which listing's average has to be redone.
    service_id = review.service_id
    db.delete(review)
    db.flush()

    if vendor:
        _recalculate_vendor_rating(vendor, db)
    _recalculate_service_rating(service_id, db)

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
