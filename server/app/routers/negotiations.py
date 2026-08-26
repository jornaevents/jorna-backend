"""Router for price negotiation endpoints."""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.dependencies import get_current_user, get_current_verified_user
from app.services.negotiation_service import (
    NegotiationError,
    start_negotiation,
    get_negotiation,
    make_offer,
    accept_offer,
    reject_offer,
)

router = APIRouter(prefix="/negotiations", tags=["negotiations"])


class StartNegotiationRequest(BaseModel):
    booking_id: str
    amount_cents: int = Field(..., gt=0, description="Proposed price in cents e.g. 150000 = $1500.00")
    message: Optional[str] = None


class MakeOfferRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, description="Counter-offer price in cents")
    message: Optional[str] = None


class RejectOfferRequest(BaseModel):
    message: Optional[str] = None


@router.post("", summary="Start a price negotiation on a booking", status_code=201)
def start_negotiation_route(
    body: StartNegotiationRequest,
    current_user=Depends(get_current_verified_user),
    db: Session = Depends(get_db),
):
    """Open a negotiation on a booking. Either the client or vendor can initiate.
    The other party can then counter, accept, or reject."""
    try:
        return start_negotiation(
            booking_id=body.booking_id,
            amount_cents=body.amount_cents,
            message=body.message,
            caller_user_id=current_user.user_id,
            db=db,
        )
    except NegotiationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/booking/{booking_id}", summary="Get negotiation status and history for a booking")
def get_negotiation_route(
    booking_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the active negotiation and full offer history for a booking."""
    try:
        return get_negotiation(
            booking_id=booking_id,
            caller_user_id=current_user.user_id,
            db=db,
        )
    except NegotiationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{negotiation_id}/offer", summary="Counter with a new price")
def make_offer_route(
    negotiation_id: str,
    body: MakeOfferRequest,
    current_user=Depends(get_current_verified_user),
    db: Session = Depends(get_db),
):
    """Submit a counter-offer. Only the party who did NOT make the last offer can counter."""
    try:
        return make_offer(
            negotiation_id=negotiation_id,
            amount_cents=body.amount_cents,
            message=body.message,
            caller_user_id=current_user.user_id,
            db=db,
        )
    except NegotiationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{negotiation_id}/accept", summary="Accept the current offer")
def accept_offer_route(
    negotiation_id: str,
    current_user=Depends(get_current_verified_user),
    db: Session = Depends(get_db),
):
    """Accept the current offer. Updates the booking price to the agreed amount."""
    try:
        return accept_offer(
            negotiation_id=negotiation_id,
            caller_user_id=current_user.user_id,
            db=db,
        )
    except NegotiationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{negotiation_id}/reject", summary="Reject and close the negotiation")
def reject_offer_route(
    negotiation_id: str,
    body: RejectOfferRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reject the current offer and close the negotiation.
    The booking price stays at the original service price."""
    try:
        return reject_offer(
            negotiation_id=negotiation_id,
            message=body.message,
            caller_user_id=current_user.user_id,
            db=db,
        )
    except NegotiationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
