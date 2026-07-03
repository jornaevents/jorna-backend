"""Router for Stripe payment and vendor onboarding endpoints."""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request
from pydantic import BaseModel, Field
from app.limiter import limiter
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.dependencies import get_current_user, get_current_admin
from app.services.stripe_service import (
    StripeError,
    create_vendor_onboarding_url,
    get_vendor_stripe_status,
    get_vendor_earnings,
    create_payment_intent,
    create_checkout_session,
    handle_stripe_webhook,
    confirm_event,
    request_refund,
    raise_dispute,
    resolve_dispute,
)

router = APIRouter(prefix="/payments", tags=["payments"])


# ── Vendor onboarding ─────────────────────────────────────────────────


@router.post(
    "/vendors/{vendor_id}/stripe-onboard",
    summary="Start Stripe Connect onboarding for a vendor",
)
def stripe_onboard(
    request: Request,
    vendor_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Returns a Stripe-hosted onboarding URL the vendor should be redirected to.
    Creates a Connect Express account if one doesn't exist yet.
    """
    try:
        return create_vendor_onboarding_url(
            vendor_id=vendor_id,
            caller_user_id=current_user.user_id,
            db=db,
            base_url=str(request.base_url),
        )
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get(
    "/vendors/{vendor_id}/stripe-status",
    summary="Check vendor Stripe onboarding status",
)
def stripe_status(
    vendor_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Returns whether the vendor has completed Stripe Connect onboarding.
    Requires authentication to prevent exposing Stripe account IDs publicly.
    """
    try:
        return get_vendor_stripe_status(vendor_id=vendor_id, caller_user_id=current_user.user_id, db=db)
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get(
    "/vendors/{vendor_id}/earnings",
    summary="Vendor earnings summary + payout history",
)
def vendor_earnings(
    vendor_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Released / in-escrow / upcoming totals plus per-booking payment history.
    Only the vendor themselves can view it."""
    try:
        return get_vendor_earnings(vendor_id=vendor_id, caller_user_id=current_user.user_id, db=db)
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# ── Payment ───────────────────────────────────────────────────────────


@router.post(
    "/bookings/{booking_id}/pay",
    summary="Initiate payment for a confirmed booking",
)
@limiter.limit("3/minute")
def pay_booking(
    request: Request,
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
        return create_payment_intent(booking_id=booking_id, caller_user_id=current_user.user_id, db=db)
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post(
    "/bookings/{booking_id}/checkout-session",
    summary="Create a hosted Stripe Checkout Session for a confirmed booking",
)
@limiter.limit("3/minute")
def create_booking_checkout_session(
    request: Request,
    booking_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Creates a Stripe-hosted Checkout Session and returns its ``checkout_url``.

    The app opens the URL in the browser; on success Stripe redirects to the
    ``/payment-complete`` page and the webhook marks the booking paid. Funds are
    held on the platform until both parties confirm the event.
    """
    try:
        return create_checkout_session(
            booking_id=booking_id,
            caller_user_id=current_user.user_id,
            base_url=str(request.base_url),
            db=db,
        )
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# ── Event confirmation & fund release ────────────────────────────────


@router.post(
    "/bookings/{booking_id}/confirm",
    summary="Confirm the event took place (customer or vendor)",
)
def confirm_booking_event(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Record that the customer or vendor confirms the event happened.
    The caller's role (customer vs vendor) is derived from their identity,
    not a client-supplied flag. When both parties have confirmed, funds are
    automatically transferred to the vendor minus the platform fee.
    """
    try:
        return confirm_event(booking_id=booking_id, caller_user_id=current_user.user_id, db=db)
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post(
    "/bookings/{booking_id}/refund",
    summary="Request a refund (within 24 hours of booking confirmation)",
)
@limiter.limit("3/minute")
def refund_booking(
    request: Request,
    booking_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Issue a full refund if the request is within 24 hours of when the
    vendor confirmed the booking. Returns 400 outside that window.
    """
    try:
        return request_refund(booking_id=booking_id, caller_user_id=current_user.user_id, db=db)
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# ── Disputes ─────────────────────────────────────────────────────────


class DisputeRequest(BaseModel):
    reason: Optional[str] = None


class ResolveDisputeRequest(BaseModel):
    resolution: str


@router.post(
    "/bookings/{booking_id}/dispute",
    summary="Raise a dispute for a paid booking",
)
@limiter.limit("3/minute")
def dispute_booking(
    request: Request,
    booking_id: str,
    body: DisputeRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Customer raises a dispute, freezing funds on the platform.
    Only allowed while payment_status is 'paid'."""
    try:
        return raise_dispute(
            booking_id=booking_id,
            caller_user_id=current_user.user_id,
            reason=body.reason,
            db=db,
        )
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post(
    "/bookings/{booking_id}/dispute/resolve",
    summary="Resolve a dispute (admin only)",
)
def resolve_booking_dispute(
    booking_id: str,
    body: ResolveDisputeRequest,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    """Admin resolves a dispute. resolution must be 'refund_customer' or 'release_vendor'."""
    try:
        return resolve_dispute(booking_id=booking_id, resolution=body.resolution, db=db)
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
