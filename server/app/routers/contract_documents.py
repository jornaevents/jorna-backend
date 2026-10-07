"""Addenda and cancellation agreements attached to a signed booking (0067,
docs/DECISIONS.md #21). The vendor's routes need a session; the couple's
(/guest-documents/{token}) don't — the token is the credential, as for a
contract (guest_bookings.py), and they're rate-limited the same way.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.dependencies import get_current_user
from app.limiter import limiter
from app.services import pdf_service
from app.utils.request_meta import client_meta
from app.services.document_service import (
    document_pdf,
    guest_document_pdf,
    DocumentServiceError,
    create_document,
    decline_by_token,
    get_by_token,
    list_documents,
    send_document,
    send_signing_code,
    sign_by_token,
    update_document,
    void_document,
)

router = APIRouter(tags=["contract-documents"])


class DocumentCreateRequest(BaseModel):
    kind: str  # addendum | cancellation
    title: Optional[str] = Field(default=None, max_length=200)
    sections: list[dict] = Field(max_length=30)
    # Send it now (status sent, link live) rather than keep it a draft.
    send: bool = False
    email_client: bool = False


class DocumentUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    sections: Optional[list[dict]] = Field(default=None, max_length=30)


class DocumentSendRequest(BaseModel):
    email_client: bool = False


class SignRequest(BaseModel):
    signer_name: str = Field(max_length=255)
    # DECISIONS #27, as on a contract.
    code: Optional[str] = Field(default=None, max_length=12)
    consent: bool = False
    consent_version: Optional[str] = Field(default=None, max_length=40)


class DeclineRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)


def _http(e: DocumentServiceError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/contracts/{booking_id}/documents", summary="Documents attached to a booking")
def list_documents_route(booking_id: str, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        return list_documents(booking_id=booking_id, caller_user_id=current_user.user_id, db=db)
    except DocumentServiceError as e:
        raise _http(e)


@router.post("/contracts/{booking_id}/documents", summary="Attach an addendum or cancellation agreement", status_code=201)
def create_document_route(
    booking_id: str, body: DocumentCreateRequest,
    current_user=Depends(get_current_user), db: Session = Depends(get_db),
):
    try:
        return create_document(
            booking_id=booking_id, caller_user_id=current_user.user_id, kind=body.kind,
            title=body.title, sections=body.sections, send=body.send, email_client=body.email_client, db=db,
        )
    except DocumentServiceError as e:
        raise _http(e)


@router.patch("/contract-documents/{document_id}", summary="Edit a document before it's signed")
def update_document_route(
    document_id: str, body: DocumentUpdateRequest,
    current_user=Depends(get_current_user), db: Session = Depends(get_db),
):
    try:
        return update_document(
            document_id=document_id, caller_user_id=current_user.user_id,
            title=body.title, sections=body.sections, db=db,
        )
    except DocumentServiceError as e:
        raise _http(e)


@router.post("/contract-documents/{document_id}/send", summary="Send (or resend) a document's link")
def send_document_route(
    document_id: str, body: Optional[DocumentSendRequest] = None,
    current_user=Depends(get_current_user), db: Session = Depends(get_db),
):
    try:
        return send_document(
            document_id=document_id, caller_user_id=current_user.user_id,
            email_client=body.email_client if body else False, db=db,
        )
    except DocumentServiceError as e:
        raise _http(e)


@router.get("/contract-documents/{document_id}/pdf", summary="An attached document as a PDF")
def document_pdf_route(document_id: str, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        return pdf_service.response(document_pdf(document_id=document_id, caller_user_id=current_user.user_id, db=db))
    except DocumentServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/contract-documents/{document_id}/void", summary="Withdraw an unsigned document")
def void_document_route(document_id: str, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        return void_document(document_id=document_id, caller_user_id=current_user.user_id, db=db)
    except DocumentServiceError as e:
        raise _http(e)


@router.get("/guest-documents/{token}", summary="Public read of an attached document")
@limiter.limit("20/minute")
def get_guest_document_route(request: Request, token: str, preview: bool = False, db: Session = Depends(get_db)):
    try:
        return get_by_token(token=token, preview=preview, db=db, client=client_meta(request))
    except DocumentServiceError as e:
        raise _http(e)


@router.get("/guest-documents/{token}/pdf", summary="The client's copy of an attached document as a PDF")
@limiter.limit("10/minute")
def guest_document_pdf_route(request: Request, token: str, db: Session = Depends(get_db)):
    try:
        return pdf_service.response(guest_document_pdf(token=token, db=db))
    except DocumentServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/guest-documents/{token}/signing-code", summary="Email the couple the code they sign with")
@limiter.limit("3/minute")
def send_document_signing_code_route(request: Request, token: str, db: Session = Depends(get_db)):
    try:
        return send_signing_code(token=token, db=db)
    except DocumentServiceError as e:
        raise _http(e)


@router.post("/guest-documents/{token}/sign", summary="The couple signs a document by typing their name")
@limiter.limit("5/minute")
def sign_guest_document_route(request: Request, token: str, body: SignRequest, db: Session = Depends(get_db)):
    try:
        return sign_by_token(
            token=token, signer_name=body.signer_name, code=body.code, consent=body.consent,
            consent_version=body.consent_version, client=client_meta(request), db=db,
        )
    except DocumentServiceError as e:
        raise _http(e)


@router.post("/guest-documents/{token}/decline", summary="The couple declines a document")
@limiter.limit("5/minute")
def decline_guest_document_route(
    request: Request, token: str, body: Optional[DeclineRequest] = None, db: Session = Depends(get_db),
):
    try:
        return decline_by_token(token=token, reason=body.reason if body else None, db=db)
    except DocumentServiceError as e:
        raise _http(e)
