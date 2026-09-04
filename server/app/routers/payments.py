"""Router for Stripe payment and vendor onboarding endpoints."""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request
from pydantic import BaseModel, Field
from app.limiter import limiter
from sqlalchemy.orm import Session

from app.config import WEB_APP_URL
from app.db.database import get_db
from app.dependencies import get_current_user, get_current_admin
from app.services.stripe_service import (
    StripeError,
    create_vendor_onboarding_url,
    get_vendor_stripe_status,
    get_vendor_earnings,
    create_payment_intent,
    create_checkout_session,
    sync_booking_payment,
    handle_stripe_webhook,
    confirm_event,
    cancel_booking,
    cancellation_preview,
    raise_dispute,
    resolve_dispute,
    create_card_setup_session,
    saved_card,
    sync_saved_card,
    forget_saved_card,
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
    client: str = Query(
        "ios",
        description="Which client is onboarding: 'ios' returns via the app "
        "deep-link bridge, 'web' returns into the Jorna web app.",
    ),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Returns a Stripe-hosted onboarding URL the vendor should be redirected to.
    Creates a Connect Express account if one doesn't exist yet.

    Same split as checkout: iOS returns to this API's own landing page, which
    bounces into the app via ``jorna://`` — a dead end in a desktop browser.
    Browser clients pass ``client=web`` to land back in the web app instead. The
    web target is WEB_APP_URL, never the request, so this can't become an open
    redirect.
    """
    return_base = WEB_APP_URL if client == "web" else str(request.base_url)
    try:
        return create_vendor_onboarding_url(
            vendor_id=vendor_id,
            caller_user_id=current_user.user_id,
            db=db,
            base_url=return_base,
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
    client: str = Query(
        "ios",
        description="Which client is paying: 'ios' returns via the app deep-link "
        "bridge, 'web' returns into the Jorna web app.",
    ),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Creates a Stripe-hosted Checkout Session and returns its ``checkout_url``.

    The client opens the URL in a browser; on success Stripe redirects to a
    ``/payment-complete`` page and the webhook marks the booking paid. Funds are
    held on the platform until both parties confirm the event.

    The return target depends on the caller. iOS returns to this API's own
    ``/payment-complete``, which bounces into the app via the ``jorna://`` URL
    scheme — a dead end in a desktop browser. Browser clients pass ``client=web``
    to land back in the web app instead. That URL comes from WEB_APP_URL, never
    from the request, so this can't be turned into an open redirect.
    """
    return_base = WEB_APP_URL if client == "web" else str(request.base_url)
    try:
        return create_checkout_session(
            booking_id=booking_id,
            caller_user_id=current_user.user_id,
            base_url=return_base,
            db=db,
        )
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post(
    "/bookings/{booking_id}/sync-payment",
    summary="Reconcile a booking's payment status directly with Stripe",
)
def sync_booking_payment_status(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Safety net for a delayed/misconfigured webhook: pull the booking's payment
    status straight from Stripe and mark it paid if the charge completed. Called
    by the app when the customer returns from hosted Checkout. Idempotent.
    """
    try:
        return sync_booking_payment(booking_id=booking_id, caller_user_id=current_user.user_id, db=db)
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
    "/bookings/{booking_id}/cancel",
    summary="Cancel a paid booking (full refund within 24h, split with the vendor after)",
)
@limiter.limit("3/minute")
def cancel_booking_route(
    request: Request,
    booking_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Full refund if this is within 24 hours of the vendor accepting.
    After that, up to the day before the event, the client gets nothing back
    and the payment splits between the platform and the vendor instead —
    see stripe_service.cancellation_split for the ramp.
    """
    try:
        return cancel_booking(booking_id=booking_id, caller_user_id=current_user.user_id, db=db)
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get(
    "/bookings/{booking_id}/cancellation-preview",
    summary="What cancelling this booking would pay out right now",
)
def cancellation_preview_route(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """The numbers behind the eligibility countdown — full-refund deadline,
    and the vendor's current cut if cancelled this instant — computed from
    the same function cancel_booking itself uses.
    """
    try:
        return cancellation_preview(
            booking_id=booking_id, caller_user_id=current_user.user_id, db=db
        )
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post(
    "/bookings/{booking_id}/reschedule-refund",
    summary="Refund a booking whose date change the vendor couldn't meet",
)
@limiter.limit("3/minute")
def reschedule_refund(
    request: Request,
    booking_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Not the ordinary 24-hour refund.

    That window is about changing your mind shortly after paying. This is about
    a vendor being unable to supply what is now being asked for, which can
    happen at any distance from the event — so it has its own gate (a declined
    or expired change request on this booking) rather than a clock, and it is
    partial: the vendor keeps a published percentage for the date they held.
    """
    from app.services.stripe_service import refund_after_failed_reschedule

    try:
        return refund_after_failed_reschedule(
            booking_id=booking_id, caller_user_id=current_user.user_id, db=db
        )
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


# ── Card on file ──────────────────────────────────────────────────────
#
# A client saves a card once, when they send their first plan, and it is charged
# when a vendor accepts. Nothing here charges anything; the charge happens on
# acceptance, in booking_service.


@router.post("/card/setup-session", summary="Start saving a card for this client")
@limiter.limit("10/minute")
def card_setup_session(
    request: Request,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """A Stripe-hosted page for entering card details, with no charge attached.

    Returns where the client lands afterwards; the web app calls
    /payments/card/sync on the way back to adopt whatever they saved.
    """
    try:
        return create_card_setup_session(
            user_id=current_user.user_id, db=db, return_base=WEB_APP_URL
        )
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/card", summary="The card on file for this client")
def card_on_file(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Brand and last four only — Stripe keeps the card itself."""
    try:
        return saved_card(user_id=current_user.user_id, db=db)
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/card/sync", summary="Adopt the card just saved")
@limiter.limit("20/minute")
def card_sync(
    request: Request,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Read the customer's cards from Stripe and keep the newest.

    Called when the client returns from the hosted form rather than trusting the
    redirect, and idempotent — arriving twice settles on the same card.
    """
    try:
        return sync_saved_card(user_id=current_user.user_id, db=db)
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete("/card", summary="Forget the card on file")
def card_forget(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Detach it at Stripe and here. Bookings already paid are unaffected —
    that money has moved and has its own refund path."""
    try:
        return forget_saved_card(user_id=current_user.user_id, db=db)
    except StripeError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
