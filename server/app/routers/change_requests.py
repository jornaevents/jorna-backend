"""Thin router for date-change endpoints — delegates to change_request_service."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.dependencies import get_current_user
from app.limiter import limiter
from app.services.change_request_service import (
    ChangeRequestError,
    consent as svc_consent,
    for_bundle as svc_for_bundle,
    propose as svc_propose,
    respond as svc_respond,
    withdraw as svc_withdraw,
)

router = APIRouter(tags=["change-requests"])


class ProposeRequest(BaseModel):
    """What the plan should move to. Every field optional — a proposal may move
    only the date, only the hours, or both — but at least one is required, which
    the service enforces so the message can say something useful."""

    date_iso: Optional[str] = Field(None, examples=["2027-06-14"])
    date_end: Optional[str] = None
    time_start: Optional[str] = Field(None, examples=["18:00"])
    time_end: Optional[str] = None
    message: Optional[str] = None


class RespondRequest(BaseModel):
    accept: bool
    message: Optional[str] = None


@router.post(
    "/bundles/{bundle_id}/change-request",
    summary="Ask every vendor on a plan to move to a new date",
)
@limiter.limit("10/minute")
def propose_change(
    request: Request,
    bundle_id: str,
    body: ProposeRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """One action, one request per agreed booking. Nothing is charged or
    refunded here — escrow moves only when a request resolves."""
    try:
        return svc_propose(
            bundle_id=bundle_id,
            caller_user_id=current_user.user_id,
            date_iso=body.date_iso,
            date_end=body.date_end,
            time_start=body.time_start,
            time_end=body.time_end,
            message=body.message,
            db=db,
        )
    except ChangeRequestError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get(
    "/bundles/{bundle_id}/change-request",
    summary="Where each vendor stands on the current proposal",
)
def get_change_requests(
    bundle_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    try:
        return svc_for_bundle(
            bundle_id=bundle_id, caller_user_id=current_user.user_id, db=db
        )
    except ChangeRequestError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete(
    "/bundles/{bundle_id}/change-request",
    summary="Withdraw every outstanding date change on a plan",
)
def withdraw_change(
    bundle_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    try:
        return svc_withdraw(
            bundle_id=bundle_id, caller_user_id=current_user.user_id, db=db
        )
    except ChangeRequestError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post(
    "/change-requests/{change_request_id}/respond",
    summary="Vendor: accept or decline a proposed new date",
)
@limiter.limit("20/minute")
def respond_to_change(
    request: Request,
    change_request_id: str,
    body: RespondRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Accepting re-checks the vendor's other bookings and refuses on a clash —
    the same guard approving a booking uses, because it's the same commitment."""
    try:
        return svc_respond(
            change_request_id=change_request_id,
            caller_user_id=current_user.user_id,
            accept=body.accept,
            message=body.message,
            db=db,
        )
    except ChangeRequestError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post(
    "/change-requests/{change_request_id}/consent",
    summary="Client: approve a reschedule that costs more",
)
def consent_to_price(
    change_request_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """The second half of an acceptance whose new dates cost more. Only the
    difference is charged; the original payment stands."""
    try:
        return svc_consent(
            change_request_id=change_request_id,
            caller_user_id=current_user.user_id,
            db=db,
        )
    except ChangeRequestError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
