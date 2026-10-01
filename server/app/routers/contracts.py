"""Router for the vendor-authenticated side of "Contracts" (vendor-authored
guest bookings) and informal Leads. The public, no-login side lives in
guest_bookings.py instead.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import Vendor
from app.dependencies import get_current_user
from app.services.contract_document import DocumentError
from app.services.contract_service import (
    ContractError,
    confirm_installment,
    create_template,
    delete_template,
    list_templates,
    propose_from_request,
    update_template,
    create_contract,
    get_contract,
    update_contract,
    void_contract,
    send_contract,
    create_lead,
    list_leads,
    update_lead,
    delete_lead,
    convert_lead,
)
from app.services.pipeline_service import PipelineError, list_pipeline, set_booking_archived

router = APIRouter(tags=["contracts"])


def _my_vendor_id(current_user, db: Session) -> str:
    vendor = db.query(Vendor).filter(Vendor.user_id == current_user.user_id).first()
    if not vendor:
        raise HTTPException(status_code=403, detail="You must be a vendor to do this")
    return vendor.vendor_id


class ContractTermsRequest(BaseModel):
    """Either line_items (the builder) or service_id + amount_cents (the
    original one-package form) — see contract_service.create_contract."""

    service_id: Optional[str] = None
    date_iso: str
    date_end: Optional[str] = None
    time_start: str
    time_end: str
    amount_cents: Optional[int] = None
    deposit_percent: Optional[int] = None
    cancellation_window_hours: Optional[int] = None
    overtime_rate_cents: Optional[int] = None
    addon_rate_cents: Optional[int] = None
    contract_terms: Optional[dict] = None
    guest_name: Optional[str] = None
    guest_email: Optional[str] = None
    guest_phone: Optional[str] = None
    location: Optional[str] = None
    # Save without sending: a draft holds no date until POST .../send.
    draft: bool = False
    # Override the vendor's hold window for this one contract.
    hold_days: Optional[int] = Field(default=None, ge=1, le=60)
    guest_count: Optional[int] = Field(default=None, ge=1)
    # Shapes are checked in contract_document, which owns them.
    line_items: Optional[list[dict]] = Field(default=None, max_length=30)
    discount_cents: Optional[int] = Field(default=None, ge=0)
    payment_schedule: Optional[list[dict]] = Field(default=None, max_length=12)
    terms_clauses: Optional[list[dict]] = Field(default=None, max_length=30)
    # Also email the client their link (ignored for a draft).
    email_client: bool = False
    # The document editor (0067): its title and blocks; terms sections in
    # the blocks become terms_clauses.
    document_title: Optional[str] = Field(default=None, max_length=200)
    document_layout: Optional[list[dict]] = Field(default=None, max_length=40)


class ContractUpdateRequest(BaseModel):
    date_iso: Optional[str] = None
    date_end: Optional[str] = None
    time_start: Optional[str] = None
    time_end: Optional[str] = None
    amount_cents: Optional[int] = None
    deposit_percent: Optional[int] = None
    cancellation_window_hours: Optional[int] = None
    overtime_rate_cents: Optional[int] = None
    addon_rate_cents: Optional[int] = None
    contract_terms: Optional[dict] = None
    guest_name: Optional[str] = None
    guest_email: Optional[str] = None
    guest_phone: Optional[str] = None
    location: Optional[str] = None
    guest_count: Optional[int] = Field(default=None, ge=1)
    line_items: Optional[list[dict]] = Field(default=None, max_length=30)
    discount_cents: Optional[int] = Field(default=None, ge=0)
    payment_schedule: Optional[list[dict]] = Field(default=None, max_length=12)
    terms_clauses: Optional[list[dict]] = Field(default=None, max_length=30)
    document_title: Optional[str] = Field(default=None, max_length=200)
    document_layout: Optional[list[dict]] = Field(default=None, max_length=40)


@router.post("/contracts", summary="Author a guest booking/contract", status_code=201)
def create_contract_route(
    body: ContractTermsRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return create_contract(
            vendor_id=_my_vendor_id(current_user, db),
            caller_user_id=current_user.user_id,
            **body.model_dump(),
            db=db,
        )
    except (ContractError, DocumentError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/contracts/{booking_id}", summary="A vendor's own view of a contract")
def get_contract_route(
    booking_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return get_contract(booking_id=booking_id, caller_user_id=current_user.user_id, db=db)
    except (ContractError, DocumentError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.patch("/contracts/{booking_id}", summary="Edit a contract's terms before it's signed")
def update_contract_route(
    booking_id: str,
    body: ContractUpdateRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        update_data = {k: v for k, v in body.model_dump().items() if v is not None}
        return update_contract(
            booking_id=booking_id, caller_user_id=current_user.user_id,
            update_data=update_data, db=db,
        )
    except (ContractError, DocumentError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/contracts/{booking_id}/void", summary="Withdraw an unsigned contract and free its date")
def void_contract_route(
    booking_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return void_contract(booking_id=booking_id, caller_user_id=current_user.user_id, db=db)
    except (ContractError, DocumentError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


class SendContractRequest(BaseModel):
    hold_days: Optional[int] = Field(default=None, ge=1, le=60)
    email_client: bool = False


@router.post("/contracts/{booking_id}/send", summary="Send a draft or resend an offer, restarting its date hold")
def send_contract_route(
    booking_id: str,
    body: Optional[SendContractRequest] = None,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return send_contract(
            booking_id=booking_id, caller_user_id=current_user.user_id, db=db,
            hold_days=body.hold_days if body else None,
            email_client=body.email_client if body else False,
        )
    except (ContractError, DocumentError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post(
    "/contracts/{booking_id}/payments/{installment_id}/confirm",
    summary="Vendor confirms one scheduled payment arrived",
)
def confirm_installment_route(
    booking_id: str,
    installment_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return confirm_installment(
            booking_id=booking_id, installment_id=installment_id,
            caller_user_id=current_user.user_id, db=db,
        )
    except (ContractError, DocumentError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


class ProposalRequest(BaseModel):
    """Accepting a marketplace request with a proposal the vendor wrote. All
    optional: whatever's left out comes from the request and the vendor's
    usual terms, as a plain accept would."""

    line_items: Optional[list[dict]] = Field(default=None, max_length=30)
    discount_cents: Optional[int] = Field(default=None, ge=0)
    payment_schedule: Optional[list[dict]] = Field(default=None, max_length=12)
    terms_clauses: Optional[list[dict]] = Field(default=None, max_length=30)
    cancellation_window_hours: Optional[int] = Field(default=None, ge=0)
    overtime_rate_cents: Optional[int] = Field(default=None, ge=0)
    hold_days: Optional[int] = Field(default=None, ge=1, le=60)
    email_client: bool = True
    document_title: Optional[str] = Field(default=None, max_length=200)
    document_layout: Optional[list[dict]] = Field(default=None, max_length=40)


@router.post(
    "/bookings/{booking_id}/propose",
    summary="Accept a marketplace request by sending the client a proposal to sign",
)
def propose_from_request_route(
    booking_id: str,
    body: ProposalRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return propose_from_request(
            booking_id=booking_id, caller_user_id=current_user.user_id, db=db, **body.model_dump(),
        )
    except (ContractError, DocumentError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# ── Templates ────────────────────────────────────────────────────────

class TemplateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    body: dict
    # agreement | addendum | cancellation (0067).
    kind: str = "agreement"


class TemplateUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    body: Optional[dict] = None
    kind: Optional[str] = None


@router.get("/contract-templates", summary="The vendor's saved contract templates")
def list_templates_route(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        return list_templates(
            vendor_id=_my_vendor_id(current_user, db), caller_user_id=current_user.user_id, db=db,
        )
    except ContractError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/contract-templates", summary="Save a contract template", status_code=201)
def create_template_route(
    body: TemplateRequest, current_user=Depends(get_current_user), db: Session = Depends(get_db),
):
    try:
        return create_template(
            vendor_id=_my_vendor_id(current_user, db), caller_user_id=current_user.user_id,
            name=body.name, body=body.body, kind=body.kind, db=db,
        )
    except ContractError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.patch("/contract-templates/{template_id}", summary="Rename or rewrite a contract template")
def update_template_route(
    template_id: str, body: TemplateUpdateRequest,
    current_user=Depends(get_current_user), db: Session = Depends(get_db),
):
    try:
        return update_template(
            template_id=template_id, caller_user_id=current_user.user_id,
            name=body.name, body=body.body, kind=body.kind, db=db,
        )
    except ContractError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete("/contract-templates/{template_id}", summary="Delete a contract template")
def delete_template_route(
    template_id: str, current_user=Depends(get_current_user), db: Session = Depends(get_db),
):
    try:
        return delete_template(template_id=template_id, caller_user_id=current_user.user_id, db=db)
    except ContractError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# ── Leads ────────────────────────────────────────────────────────────

class LeadCreateRequest(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    event_date_iso: Optional[str] = None
    note: Optional[str] = None


class LeadUpdateRequest(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    event_date_iso: Optional[str] = None
    note: Optional[str] = None
    status: Optional[str] = None
    # Hide from (true) or return to (false) the vendor's active leads.
    archived: Optional[bool] = None


@router.get("/leads/pipeline", summary="Everything before a signed contract, in one list")
def pipeline_route(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Marketplace requests, unsigned contracts and leads, each labelled
    inquiry or negotiation, with why it needs the vendor (pipeline_service)."""
    try:
        return list_pipeline(caller_user_id=current_user.user_id, db=db)
    except PipelineError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


class ArchiveRequest(BaseModel):
    archived: bool = True


@router.post("/bookings/{booking_id}/archive", summary="Hide a request or unsigned contract from your active leads")
def archive_booking_route(
    booking_id: str,
    body: Optional[ArchiveRequest] = None,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """archived=false brings it back. Declines, voids and notifies nothing."""
    try:
        return set_booking_archived(
            booking_id=booking_id, archived=body.archived if body else True,
            caller_user_id=current_user.user_id, db=db,
        )
    except PipelineError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/leads", summary="Log an informal, off-platform prospect", status_code=201)
def create_lead_route(
    body: LeadCreateRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return create_lead(
            vendor_id=_my_vendor_id(current_user, db),
            caller_user_id=current_user.user_id,
            **body.model_dump(),
            db=db,
        )
    except (ContractError, DocumentError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/leads", summary="List the authenticated vendor's leads")
def list_leads_route(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return list_leads(
            vendor_id=_my_vendor_id(current_user, db),
            caller_user_id=current_user.user_id,
            db=db,
        )
    except (ContractError, DocumentError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.patch("/leads/{lead_id}", summary="Edit a lead")
def update_lead_route(
    lead_id: str,
    body: LeadUpdateRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        update_data = {k: v for k, v in body.model_dump().items() if v is not None}
        return update_lead(
            lead_id=lead_id, caller_user_id=current_user.user_id,
            update_data=update_data, db=db,
        )
    except (ContractError, DocumentError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete("/leads/{lead_id}", summary="Delete a lead")
def delete_lead_route(
    lead_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return delete_lead(lead_id=lead_id, caller_user_id=current_user.user_id, db=db)
    except (ContractError, DocumentError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/leads/{lead_id}/convert", summary="Turn a lead into a real contract", status_code=201)
def convert_lead_route(
    lead_id: str,
    body: ContractTermsRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return convert_lead(
            lead_id=lead_id,
            caller_user_id=current_user.user_id,
            **body.model_dump(),
            db=db,
        )
    except (ContractError, DocumentError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
