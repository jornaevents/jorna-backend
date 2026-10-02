"""Public, unauthenticated router for the client-facing side of a guest/
contract booking — see docs/DECISIONS.md #13. No Depends(get_current_user)
anywhere in this file, deliberately: the contract_token in the path is the
entire credential, same trust model as the existing RSVP system's public
`GET /guests/invitation`. Rate-limited more aggressively than most
endpoints in this app precisely because there's no account behind any of
these calls to throttle by identity instead.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.limiter import limiter
from app.services import pdf_service, proposal_service
from app.services.contract_document import DocumentError
from app.services.contract_service import ContractError
from app.services.guest_booking_service import (
    GuestBookingError,
    get_guest_booking,
    guest_contract_pdf,
    fill_details,
    sign_contract,
    decline_contract,
    mark_full_paid,
    mark_installment_paid,
    mark_deposit_paid,
)

router = APIRouter(prefix="/guest-bookings", tags=["guest-bookings"])


@router.get("/{contract_token}", summary="Public read of a guest booking/contract")
@limiter.limit("20/minute")
def get_guest_booking_route(
    request: Request,
    contract_token: str,
    preview: bool = False,
    db: Session = Depends(get_db),
):
    """preview=true is the vendor's "View as client" — it doesn't mark the
    contract viewed."""
    try:
        return get_guest_booking(contract_token=contract_token, db=db, preview=preview)
    except GuestBookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


class FillDetailsRequest(BaseModel):
    guest_name: Optional[str] = None
    guest_email: Optional[str] = None
    guest_phone: Optional[str] = None
    location: Optional[str] = None
    guest_count: Optional[int] = None


@router.get("/{contract_token}/pdf", summary="The client's copy as a PDF")
@limiter.limit("10/minute")
def guest_contract_pdf_route(request: Request, contract_token: str, db: Session = Depends(get_db)):
    try:
        return pdf_service.response(guest_contract_pdf(contract_token=contract_token, db=db))
    except GuestBookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.patch("/{contract_token}", summary="Client fills in their own contact + venue details")
@limiter.limit("10/minute")
def fill_details_route(
    request: Request,
    contract_token: str,
    body: FillDetailsRequest,
    db: Session = Depends(get_db),
):
    try:
        return fill_details(contract_token=contract_token, db=db, **body.model_dump())
    except GuestBookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


class SignRequest(BaseModel):
    signer_name: str
    # The revision the client read (GET returns it). Refused if the vendor
    # has edited since. Optional so older pages keep working.
    revision: Optional[int] = None


@router.post("/{contract_token}/sign", summary="Client e-signs by typing their full legal name")
@limiter.limit("5/minute")
def sign_contract_route(
    request: Request,
    contract_token: str,
    body: SignRequest,
    db: Session = Depends(get_db),
):
    try:
        return sign_contract(
            contract_token=contract_token, signer_name=body.signer_name, revision=body.revision, db=db,
        )
    except GuestBookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{contract_token}/mark-paid", summary="Client: mark the full/remaining balance as paid")
@limiter.limit("10/minute")
def mark_full_paid_route(
    request: Request,
    contract_token: str,
    db: Session = Depends(get_db),
):
    try:
        return mark_full_paid(contract_token=contract_token, db=db)
    except GuestBookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{contract_token}/mark-deposit-paid", summary="Client: mark the deposit as paid")
@limiter.limit("10/minute")
def mark_deposit_paid_route(
    request: Request,
    contract_token: str,
    db: Session = Depends(get_db),
):
    try:
        return mark_deposit_paid(contract_token=contract_token, db=db)
    except GuestBookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


class DeclineRequest(BaseModel):
    reason: Optional[str] = None


@router.post("/{contract_token}/decline", summary="Client turns the offer down, freeing the vendor's date")
@limiter.limit("5/minute")
def decline_contract_route(
    request: Request,
    contract_token: str,
    body: Optional[DeclineRequest] = None,
    db: Session = Depends(get_db),
):
    try:
        return decline_contract(
            contract_token=contract_token, reason=body.reason if body else None, db=db,
        )
    except GuestBookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post(
    "/{contract_token}/payments/{installment_id}/mark-paid",
    summary="Client: mark one scheduled payment as sent",
)
@limiter.limit("10/minute")
def mark_installment_paid_route(
    request: Request,
    contract_token: str,
    installment_id: str,
    db: Session = Depends(get_db),
):
    try:
        return mark_installment_paid(contract_token=contract_token, installment_id=installment_id, db=db)
    except GuestBookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)



# ── Change proposals (docs/DECISIONS.md #23) ─────────────────────────


class ProposeRequest(BaseModel):
    # The revision the client was reading; refused if the vendor has edited since.
    base_revision: int
    # Any of the contract's terms (contract_document.TERMS_FIELDS); what's
    # left out stays as it is.
    changes: dict
    message: Optional[str] = None


@router.get("/{contract_token}/proposals", summary="The client's change proposals and the versions they compare")
@limiter.limit("30/minute")
def list_proposals_route(request: Request, contract_token: str, db: Session = Depends(get_db)):
    try:
        return proposal_service.guest_history(contract_token=contract_token, db=db)
    except GuestBookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{contract_token}/proposals", summary="Client proposes changes to an unsigned contract", status_code=201)
@limiter.limit("10/minute")
def propose_route(request: Request, contract_token: str, body: ProposeRequest, db: Session = Depends(get_db)):
    try:
        return proposal_service.propose(
            contract_token=contract_token, base_revision=body.base_revision,
            changes=body.changes, message=body.message, db=db,
        )
    except (GuestBookingError, ContractError, DocumentError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/{contract_token}/proposals/{proposal_id}/withdraw", summary="Client takes back an open proposal")
@limiter.limit("10/minute")
def withdraw_proposal_route(request: Request, contract_token: str, proposal_id: str, db: Session = Depends(get_db)):
    try:
        return proposal_service.withdraw(contract_token=contract_token, proposal_id=proposal_id, db=db)
    except GuestBookingError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
