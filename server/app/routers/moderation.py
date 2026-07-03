"""Content reporting and user blocking (App Store guideline 1.2).

POST   /reports            — file a report against content or a user
GET    /blocks             — list users I've blocked
POST   /blocks/{user_id}   — block a user
DELETE /blocks/{user_id}   — unblock a user

Blocking is enforced client-side: the app fetches /blocks and filters the
blocked users' vendors, conversations, messages, and reviews out of view.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import ContentReport, User, UserBlock, Vendor
from app.dependencies import get_current_user

router = APIRouter(tags=["moderation"])

_REPORT_TARGET_TYPES = {"user", "vendor", "review", "message", "conversation"}
_REPORT_REASONS = {"spam", "inappropriate", "harassment", "scam", "other"}


class CreateReportRequest(BaseModel):
    target_type: str
    target_id: str = Field(..., min_length=1, max_length=36)
    reason: str
    details: Optional[str] = Field(None, max_length=2000)

    @field_validator("target_type")
    @classmethod
    def valid_target_type(cls, v: str) -> str:
        if v not in _REPORT_TARGET_TYPES:
            raise ValueError(f"target_type must be one of: {', '.join(sorted(_REPORT_TARGET_TYPES))}")
        return v

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, v: str) -> str:
        if v not in _REPORT_REASONS:
            raise ValueError(f"reason must be one of: {', '.join(sorted(_REPORT_REASONS))}")
        return v


@router.post("/reports", status_code=201, summary="Report content or a user")
def create_report(
    body: CreateReportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    report = ContentReport(
        reporter_user_id=current_user.user_id,
        target_type=body.target_type,
        target_id=body.target_id,
        reason=body.reason,
        details=body.details,
        status="open",
        created_at=datetime.now(timezone.utc),
    )
    db.add(report)
    db.commit()
    return {"message": "Report received. Our team will review it.", "report_id": report.report_id}


@router.get("/blocks", summary="List users I've blocked")
def list_blocks(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    blocks = (
        db.query(UserBlock)
        .filter(UserBlock.blocker_user_id == current_user.user_id)
        .order_by(UserBlock.created_at.desc())
        .all()
    )
    blocked_ids = [b.blocked_user_id for b in blocks]
    users = {
        u.user_id: u
        for u in db.query(User).filter(User.user_id.in_(blocked_ids)).all()
    } if blocked_ids else {}
    # Include the blocked user's vendor_id (when they are a vendor) so the app
    # can filter vendor listings without an extra lookup.
    vendor_by_user = {
        v.user_id: v.vendor_id
        for v in db.query(Vendor).filter(Vendor.user_id.in_(blocked_ids)).all()
    } if blocked_ids else {}
    return [
        {
            "blocked_user_id": b.blocked_user_id,
            "blocked_name": (
                f"{users[b.blocked_user_id].f_name} {users[b.blocked_user_id].l_name}"
                if b.blocked_user_id in users else "Deleted user"
            ),
            "blocked_vendor_id": vendor_by_user.get(b.blocked_user_id),
            "created_at": b.created_at.isoformat(),
        }
        for b in blocks
    ]


@router.post("/blocks/{user_id}", status_code=201, summary="Block a user")
def block_user(
    user_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user_id == current_user.user_id:
        raise HTTPException(status_code=400, detail="You can't block yourself")
    target = db.query(User).filter(User.user_id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    existing = (
        db.query(UserBlock)
        .filter(
            UserBlock.blocker_user_id == current_user.user_id,
            UserBlock.blocked_user_id == user_id,
        )
        .first()
    )
    if existing:
        return {"message": "Already blocked"}
    db.add(UserBlock(
        blocker_user_id=current_user.user_id,
        blocked_user_id=user_id,
        created_at=datetime.now(timezone.utc),
    ))
    db.commit()
    return {"message": "User blocked"}


@router.delete("/blocks/{user_id}", summary="Unblock a user")
def unblock_user(
    user_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    deleted = (
        db.query(UserBlock)
        .filter(
            UserBlock.blocker_user_id == current_user.user_id,
            UserBlock.blocked_user_id == user_id,
        )
        .delete()
    )
    db.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="Not blocked")
    return {"message": "User unblocked"}
