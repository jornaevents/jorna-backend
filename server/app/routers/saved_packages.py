"""Saved packages (0090): the packages a client hearts in Explore, and the facts
their side-by-side comparison shows. Replaces saved vendors for Compare — see
SavedPackage for why the package, not the vendor, is the thing compared."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import SavedPackage, Service, User, Vendor
from app.dependencies import get_current_user
from app.services.names import vendor_display_name
from app.services.vendor_service import _service_cover_url

router = APIRouter(prefix="/saved-packages", tags=["saved-packages"])

# Enough for any real shortlist; a cap so the table can't be used as storage.
MAX_SAVED = 50


def _summary(service: Service, vendor: Vendor, user: User | None, saved_at: datetime) -> dict:
    return {
        "service_id": service.service_id,
        "name": service.name,
        # A package can be hidden or archived after it was saved. It stays on the
        # shortlist so it doesn't silently vanish, and the client says so.
        "available": service.status == "active",
        "category": service.category or vendor.category,
        "subcategory": service.subcategory or vendor.subcategory,
        "photo_url": _service_cover_url(service.media) or (user.pfp_url if user else None),
        "price": service.price,
        "price_unit": service.price_unit,
        "included_hours": service.included_hours,
        "inclusions": service.inclusions or [],
        "add_ons": [a.get("name") for a in (service.add_ons or []) if isinstance(a, dict) and a.get("name")],
        "deposit_percent": service.deposit_percent,
        "cancellation_window_hours": service.cancellation_window_hours,
        "negotiable": bool(service.negotiable),
        # This package's own record, not the vendor's blend.
        "rating": service.rating,
        "num_reviews": service.num_reviews,
        "vendor_id": vendor.vendor_id,
        "vendor_name": vendor_display_name(vendor, user, "Vendor"),
        "vendor_location": user.location if user else None,
        "years_experience": vendor.years_experience,
        "saved_at": saved_at.isoformat(),
    }


@router.get("", summary="The caller's saved packages, newest first")
def list_saved(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (
        db.query(SavedPackage, Service, Vendor, User)
        .join(Service, Service.service_id == SavedPackage.service_id)
        .join(Vendor, Vendor.vendor_id == Service.vendor_id)
        .outerjoin(User, User.user_id == Vendor.user_id)
        .filter(SavedPackage.user_id == current_user.user_id)
        .order_by(SavedPackage.created_at.desc())
        .all()
    )
    return {"items": [_summary(svc, v, u, s.created_at) for s, svc, v, u in rows]}


@router.put("/{service_id}", summary="Save a package", status_code=204)
def save_package(service_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Only what Explore can show: a hidden or archived package isn't on offer.
    if not db.query(Service.service_id).filter(Service.service_id == service_id, Service.status == "active").first():
        raise HTTPException(status_code=404, detail="Package not found")
    mine = db.query(SavedPackage).filter(SavedPackage.user_id == current_user.user_id)
    # Saving twice is the same as saving once.
    if mine.filter(SavedPackage.service_id == service_id).first():
        return
    if mine.count() >= MAX_SAVED:
        raise HTTPException(status_code=400, detail=f"You can save up to {MAX_SAVED} packages — remove one first")
    db.add(SavedPackage(user_id=current_user.user_id, service_id=service_id))
    db.commit()


@router.delete("/{service_id}", summary="Remove a saved package", status_code=204)
def unsave_package(service_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.query(SavedPackage).filter(
        SavedPackage.user_id == current_user.user_id, SavedPackage.service_id == service_id
    ).delete()
    db.commit()
