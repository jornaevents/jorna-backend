"""Router for the vendor-authenticated side of "Contracts" (vendor-authored
guest bookings) and informal Leads. The public, no-login side lives in
guest_bookings.py instead.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import Vendor
from app.dependencies import get_current_user
from app.services.contract_service import (
    ContractError,
    create_contract,
    get_contract,
    update_contract,
    void_contract,
    create_lead,
    list_leads,
    update_lead,
    delete_lead,
    convert_lead,
)

router = APIRouter(tags=["contracts"])


def _my_vendor_id(current_user, db: Session) -> str:
    vendor = db.query(Vendor).filter(Vendor.user_id == current_user.user_id).first()
    if not vendor:
        raise HTTPException(status_code=403, detail="You must be a vendor to do this")
    return vendor.vendor_id


class ContractTermsRequest(BaseModel):
    service_id: str
    date_iso: str
    date_end: Optional[str] = None
    time_start: str
    time_end: str
    amount_cents: int
    deposit_percent: Optional[int] = None
    cancellation_window_hours: Optional[int] = None
    overtime_rate_cents: Optional[int] = None
    addon_rate_cents: Optional[int] = None
    contract_terms: Optional[dict] = None
    guest_name: Optional[str] = None
    guest_email: Optional[str] = None
    guest_phone: Optional[str] = None
    location: Optional[str] = None


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
    except ContractError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/contracts/{booking_id}", summary="A vendor's own view of a contract")
def get_contract_route(
    booking_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return get_contract(booking_id=booking_id, caller_user_id=current_user.user_id, db=db)
    except ContractError as e:
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
    except ContractError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/contracts/{booking_id}/void", summary="Withdraw an unsigned contract and free its date")
def void_contract_route(
    booking_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return void_contract(booking_id=booking_id, caller_user_id=current_user.user_id, db=db)
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
    except ContractError as e:
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
    except ContractError as e:
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
    except ContractError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete("/leads/{lead_id}", summary="Delete a lead")
def delete_lead_route(
    lead_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return delete_lead(lead_id=lead_id, caller_user_id=current_user.user_id, db=db)
    except ContractError as e:
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
    except ContractError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
