"""Saved vendors (0088): the vendors a client hearts in Explore, and the
facts their side-by-side comparison shows. Small enough that the queries live
here rather than in a service."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import SavedVendor, Service, User, Vendor
from app.dependencies import get_current_user
from app.services.names import vendor_display_name

router = APIRouter(prefix="/saved-vendors", tags=["saved-vendors"])

# Enough for any real shortlist; a cap so the table can't be used as storage.
MAX_SAVED = 50


def _summary(vendor: Vendor, user: User | None, starting_price: float | None, saved_at: datetime) -> dict:
    return {
        "vendor_id": vendor.vendor_id,
        "name": vendor_display_name(vendor, user, "Vendor"),
        "category": vendor.category,
        "subcategory": vendor.subcategory,
        "pfp_url": user.pfp_url if user else None,
        "location": user.location if user else None,
        "rating": vendor.rating,
        # Vendor.num_events counts the vendor's reviews (see Service.num_reviews).
        "num_reviews": vendor.num_events,
        "years_experience": vendor.years_experience,
        # The cheapest publicly listed package, as a rate — its unit isn't
        # carried, so clients show it as "from".
        "starting_price": starting_price,
        "saved_at": saved_at.isoformat(),
    }


@router.get("", summary="The caller's saved vendors, newest first")
def list_saved(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (
        db.query(SavedVendor, Vendor, User)
        .join(Vendor, Vendor.vendor_id == SavedVendor.vendor_id)
        .outerjoin(User, User.user_id == Vendor.user_id)
        .filter(SavedVendor.user_id == current_user.user_id)
        .order_by(SavedVendor.created_at.desc())
        .all()
    )
    ids = [v.vendor_id for _, v, _ in rows]
    cheapest = dict(
        db.query(Service.vendor_id, func.min(Service.price))
        .filter(Service.vendor_id.in_(ids), Service.status == "active")
        .group_by(Service.vendor_id)
        .all()
    ) if ids else {}
    return {"items": [_summary(v, u, cheapest.get(v.vendor_id), s.created_at) for s, v, u in rows]}


@router.put("/{vendor_id}", summary="Save a vendor", status_code=204)
def save_vendor(vendor_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not db.query(Vendor.vendor_id).filter(Vendor.vendor_id == vendor_id).first():
        raise HTTPException(status_code=404, detail="Vendor not found")
    mine = db.query(SavedVendor).filter(SavedVendor.user_id == current_user.user_id)
    # Saving twice is the same as saving once.
    if mine.filter(SavedVendor.vendor_id == vendor_id).first():
        return
    if mine.count() >= MAX_SAVED:
        raise HTTPException(status_code=400, detail=f"You can save up to {MAX_SAVED} vendors — remove one first")
    db.add(SavedVendor(user_id=current_user.user_id, vendor_id=vendor_id))
    db.commit()


@router.delete("/{vendor_id}", summary="Remove a saved vendor", status_code=204)
def unsave_vendor(vendor_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.query(SavedVendor).filter(
        SavedVendor.user_id == current_user.user_id, SavedVendor.vendor_id == vendor_id
    ).delete()
    db.commit()
