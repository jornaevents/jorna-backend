"""Admin-only endpoints for user management and data maintenance."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User
from app.dependencies import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

@router.post("/users/{user_id}/make-admin", summary="Promote a user to admin")
def make_admin(
    user_id: str,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Grant admin privileges to a user. Requires admin auth."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.is_admin:
        return {"message": f"{user.email} is already an admin"}
    user.is_admin = True
    db.commit()
    return {"message": f"{user.email} has been promoted to admin", "user_id": user.user_id}


@router.post("/users/{user_id}/revoke-admin", summary="Revoke admin privileges from a user")
def revoke_admin(
    user_id: str,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Revoke admin privileges from a user. Admins cannot revoke their own access."""
    if user_id == current_admin.user_id:
        raise HTTPException(status_code=400, detail="You cannot revoke your own admin access")
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.is_admin:
        return {"message": f"{user.email} is not an admin"}
    user.is_admin = False
    db.commit()
    return {"message": f"{user.email} has had admin access revoked", "user_id": user.user_id}


@router.get("/users", summary="List all admin users")
def list_admins(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Return all users with admin privileges."""
    admins = db.query(User).filter(User.is_admin == True).all()
    return [{"user_id": u.user_id, "email": u.email, "f_name": u.f_name, "l_name": u.l_name} for u in admins]


@router.get("/reports", summary="List content reports (admin only)")
def list_reports(
    status: str | None = Query(None, description="Filter: open | reviewed | dismissed"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    from app.db.models import ContentReport
    q = db.query(ContentReport)
    if status:
        q = q.filter(ContentReport.status == status)
    rows = q.order_by(ContentReport.created_at.desc()).offset(offset).limit(limit).all()
    return [
        {
            "report_id": r.report_id,
            "reporter_user_id": r.reporter_user_id,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "reason": r.reason,
            "details": r.details,
            "status": r.status,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.patch("/reports/{report_id}", summary="Update a report's status (admin only)")
def update_report_status(
    report_id: str,
    status: str = Query(..., description="open | reviewed | dismissed"),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    from app.db.models import ContentReport
    if status not in {"open", "reviewed", "dismissed"}:
        raise HTTPException(status_code=400, detail="status must be open, reviewed, or dismissed")
    report = db.query(ContentReport).filter(ContentReport.report_id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    report.status = status
    db.commit()
    return {"message": "Updated", "report_id": report_id, "status": status}


@router.post("/cleanup/bundle-event-data", summary="One-off cleanup of legacy bundle/event data (admin only)")
def cleanup_bundle_event_data(
    dry_run: bool = Query(True, description="Preview what would be deleted without modifying anything"),
    stale_days: int = Query(7, ge=1, le=365, description="Draft comparison bundles older than this many days are considered abandoned"),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Remove data artifacts from before the 2026-07 bundle→event linkage fix:
    orphan AI-created events, abandoned comparison-draft bundles, and duplicate
    bookings. Run with dry_run=true first and review the report; paid bookings
    are never touched either way."""
    from app.services.bundle_service import cleanup_legacy_bundle_event_data
    return cleanup_legacy_bundle_event_data(db=db, dry_run=dry_run, stale_days=stale_days)
