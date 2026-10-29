"""The vendor's countersignature on a contract or an attached document the
client has already signed (docs/DECISIONS.md #34). Vendor-authenticated;
the client's side is in guest_bookings.py and contract_documents.py."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.dependencies import get_current_user
from app.services.countersign_service import (
    CountersignError,
    countersign_contract,
    countersign_document,
    decline_contract_signature,
    decline_document_signature,
)
from app.utils.request_meta import client_meta

router = APIRouter(tags=["contracts"])


class CountersignRequest(BaseModel):
    signer_name: str = Field(max_length=255)
    # Needed only the first time, or after the consent's wording changes:
    # the contract's vendor_esign_consent.given says which.
    consent: bool = False
    consent_version: Optional[str] = None
    # A name that isn't the one on the vendor's account signs only once
    # they've confirmed it (countersign_service._check_name).
    name_confirmed: bool = False


class DeclineSignatureRequest(BaseModel):
    reason: str = Field(max_length=500)


def _http(e: CountersignError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/contracts/{booking_id}/countersign", summary="The vendor signs a contract the client signed")
def countersign_contract_route(
    booking_id: str,
    body: CountersignRequest,
    request: Request,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return countersign_contract(
            booking_id=booking_id, caller_user_id=current_user.user_id, signer_name=body.signer_name,
            consent=body.consent, consent_version=body.consent_version, client=client_meta(request),
            name_confirmed=body.name_confirmed, db=db,
        )
    except CountersignError as e:
        raise _http(e)


@router.post(
    "/contracts/{booking_id}/decline-signature",
    summary="The vendor won't sign a contract the client signed; it's voided and the client told why",
)
def decline_contract_signature_route(
    booking_id: str,
    body: DeclineSignatureRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return decline_contract_signature(
            booking_id=booking_id, caller_user_id=current_user.user_id, reason=body.reason, db=db,
        )
    except CountersignError as e:
        raise _http(e)


@router.post(
    "/contract-documents/{document_id}/countersign",
    summary="The vendor signs an attached document the client signed",
)
def countersign_document_route(
    document_id: str,
    body: CountersignRequest,
    request: Request,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return countersign_document(
            document_id=document_id, caller_user_id=current_user.user_id, signer_name=body.signer_name,
            consent=body.consent, consent_version=body.consent_version, client=client_meta(request),
            name_confirmed=body.name_confirmed, db=db,
        )
    except CountersignError as e:
        raise _http(e)


@router.post(
    "/contract-documents/{document_id}/decline-signature",
    summary="The vendor won't sign an attached document the client signed",
)
def decline_document_signature_route(
    document_id: str,
    body: DeclineSignatureRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return decline_document_signature(
            document_id=document_id, caller_user_id=current_user.user_id, reason=body.reason, db=db,
        )
    except CountersignError as e:
        raise _http(e)
