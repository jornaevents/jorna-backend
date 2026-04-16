"""Router for Stripe payment and vendor onboarding endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.dependencies import get_current_user
from app.services.stripe_service import (
    StripeError,
    create_vendor_onboarding_url,
    get_vendor_stripe_status,
    create_payment_intent,
    handle_stripe_webhook,
    confirm_event,
    request_refund,
)

router = APIRouter(prefix="/payments", tags=["payments"])


# ── Vendor onboarding ─────────────────────────────────────────────────


@router.post(
    "/vendors/{vendor_id}/stripe-onboard",
    summary="Start Stripe Connect onboarding for a vendor",
)
def stripe_onboard(
    vendor_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Returns a Stripe-hosted onboarding URL the vendor should be redirected to.
    Creates a Connect Express account if one doesn't exist yet.
    """
    try:
        return create_vendor_onboarding_url(vendor_id=vendor_id, db=db)
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get(
    "/vendors/{vendor_id}/stripe-status",
    summary="Check vendor Stripe onboarding status",
)
def stripe_status(
    vendor_id: str,
    db: Session = Depends(get_db),
):
    """Returns whether the vendor has completed Stripe Connect onboarding
    and is able to receive payouts.
    """
    try:
        return get_vendor_stripe_status(vendor_id=vendor_id, db=db)
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# ── Payment ───────────────────────────────────────────────────────────


@router.post(
    "/bookings/{booking_id}/pay",
    summary="Initiate payment for a confirmed booking",
)
def pay_booking(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Creates a Stripe PaymentIntent for a confirmed booking.

    Returns a `client_secret` the frontend passes to Stripe.js to collect
    the customer's card and complete the payment. Funds are held in the
    Desiconnect platform balance until both parties confirm the event.
    """
    try:
        return create_payment_intent(booking_id=booking_id, db=db)
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# ── Event confirmation & fund release ────────────────────────────────


@router.post(
    "/bookings/{booking_id}/confirm",
    summary="Confirm the event took place (customer or vendor)",
)
def confirm_booking_event(
    booking_id: str,
    is_vendor: bool = Query(..., description="True if the caller is the vendor"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Record that the customer or vendor confirms the event happened.
    When both parties have confirmed, funds are automatically transferred
    to the vendor minus the platform fee.
    """
    try:
        return confirm_event(booking_id=booking_id, is_vendor=is_vendor, db=db)
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post(
    "/bookings/{booking_id}/refund",
    summary="Request a refund (within 24 hours of booking confirmation)",
)
def refund_booking(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Issue a full refund if the request is within 24 hours of when the
    vendor confirmed the booking. Returns 400 outside that window.
    """
    try:
        return request_refund(booking_id=booking_id, db=db)
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# ── Webhook ───────────────────────────────────────────────────────────


@router.post("/webhook", summary="Stripe webhook receiver", include_in_schema=False)
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(..., alias="stripe-signature"),
    db: Session = Depends(get_db),
):
    """Receives and verifies Stripe webhook events.

    Must receive the raw request body (not parsed JSON) for signature
    verification to work — FastAPI's Request.body() handles this correctly.
    """
    payload = await request.body()
    try:
        return handle_stripe_webhook(payload=payload, signature=stripe_signature, db=db)
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
