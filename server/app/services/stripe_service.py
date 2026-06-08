"""Business logic for Stripe Connect vendor onboarding and payments."""

import logging
from datetime import datetime, timedelta, timezone

import stripe

from sqlalchemy.orm import Session

from app.config import STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, FRONTEND_URL, PLATFORM_FEE_PERCENT
from app.db.models import Vendor, Booking, Service, StripeWebhookEvent, User

stripe.api_key = STRIPE_SECRET_KEY

logger = logging.getLogger(__name__)


class StripeError(Exception):
    """Raised when a Stripe operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ── Vendor onboarding ─────────────────────────────────────────────────


def create_vendor_onboarding_url(*, vendor_id: str, caller_user_id: str, db: Session) -> dict:
    """Create (or reuse) a Stripe Express Connect account for the vendor
    and return a one-time hosted onboarding URL.

    If the vendor already has a stripe_account_id, a fresh Account Link is
    generated for the same account (handles the case where the vendor
    didn't finish onboarding the first time).
    """
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise StripeError(404, "Vendor not found")
    if vendor.user_id != caller_user_id:
        raise StripeError(403, "You are not authorised to onboard this vendor")

    try:
        if not vendor.stripe_account_id:
            user = db.query(User).filter(User.user_id == vendor.user_id).first()
            account = stripe.Account.create(
                type="express",
                email=user.email if user else None,
                capabilities={
                    "card_payments": {"requested": True},
                    "transfers": {"requested": True},
                },
            )
            vendor.stripe_account_id = account.id
            db.commit()
            logger.info("Created Stripe Connect account %s for vendor %s", account.id, vendor_id)

        account_link = stripe.AccountLink.create(
            account=vendor.stripe_account_id,
            refresh_url=f"{FRONTEND_URL}/vendor/stripe-onboard/refresh?vendor_id={vendor_id}",
            return_url=f"{FRONTEND_URL}/vendor/stripe-onboard/return?vendor_id={vendor_id}",
            type="account_onboarding",
        )
    except stripe.StripeError as e:
        raise StripeError(502, f"Stripe error: {e.user_message or str(e)}")

    return {"onboarding_url": account_link.url}


def get_vendor_stripe_status(*, vendor_id: str, caller_user_id: str, db: Session) -> dict:
    """Check whether the vendor has completed Stripe Connect onboarding.

    Queries the Stripe API for the latest status and syncs it to the DB.
    """
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise StripeError(404, "Vendor not found")
    if vendor.user_id != caller_user_id:
        raise StripeError(403, "You are not authorised to view this vendor's Stripe status")

    if not vendor.stripe_account_id:
        return {"stripe_account_id": None, "stripe_onboarding_complete": False}

    try:
        account = stripe.Account.retrieve(vendor.stripe_account_id)
        complete = bool(account.details_submitted)

        if complete != vendor.stripe_onboarding_complete:
            vendor.stripe_onboarding_complete = complete
            db.commit()
            logger.info(
                "Updated stripe_onboarding_complete=%s for vendor %s", complete, vendor_id
            )
    except stripe.StripeError as e:
        raise StripeError(502, f"Stripe error: {e.user_message or str(e)}")

    return {
        "stripe_account_id": vendor.stripe_account_id,
        "stripe_onboarding_complete": complete,
    }


# ── Payment intent ────────────────────────────────────────────────────


def create_payment_intent(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    """Create a Stripe PaymentIntent for a confirmed booking.

    The charge lands in the Desiconnect platform balance (not sent directly
    to the vendor). Funds are held there until both parties confirm the
    event is complete, at which point a Transfer is issued separately.

    Returns the PaymentIntent client_secret so the frontend can complete
    the card collection with Stripe.js.
    """
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise StripeError(404, "Booking not found")

    if booking.user_id != caller_user_id:
        raise StripeError(403, "You are not the customer for this booking")

    if booking.status != "approved":
        raise StripeError(400, "Payment can only be initiated for approved bookings")

    if booking.payment_status != "unpaid":
        raise StripeError(400, f"Booking payment is already '{booking.payment_status}'")

    # Look up the service to get the price
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    if not service:
        raise StripeError(404, "Service not found")

    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    if not vendor or not vendor.stripe_onboarding_complete:
        raise StripeError(
            400,
            "This vendor has not completed Stripe onboarding and cannot accept payments yet.",
        )

    # Use the negotiated price if one was agreed, otherwise fall back to the listed service price.
    amount_cents = booking.amount_cents if booking.amount_cents else round(service.price * 100)
    platform_fee_cents = round(amount_cents * PLATFORM_FEE_PERCENT / 100)

    try:
        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency=booking.currency,
            # Disable redirect-based methods (Klarna, Affirm etc.) so no
            # return_url is required. Card payments still work fine.
            automatic_payment_methods={
                "enabled": True,
                "allow_redirects": "never",
            },
            # transfer_group links this charge to future Transfer calls so Stripe
            # can track the full money flow for the booking.
            transfer_group=booking_id,
            metadata={
                "booking_id": booking_id,
                "vendor_id": booking.vendor_id,
                "user_id": booking.user_id,
            },
        )
    except stripe.StripeError as e:
        raise StripeError(502, f"Stripe error: {e.user_message or str(e)}")

    # Persist intent details on the booking
    booking.payment_intent_id = intent.id
    booking.payment_status = "processing"
    booking.amount_cents = amount_cents
    booking.platform_fee_cents = platform_fee_cents
    db.commit()

    logger.info(
        "Created PaymentIntent %s for booking %s (%d cents)", intent.id, booking_id, amount_cents
    )

    return {
        "client_secret": intent.client_secret,
        "payment_intent_id": intent.id,
        "amount_cents": amount_cents,
        "platform_fee_cents": platform_fee_cents,
        "currency": booking.currency,
    }


# ── Webhook ───────────────────────────────────────────────────────────


def handle_stripe_webhook(*, payload: bytes, signature: str, db: Session) -> dict:
    """Verify and process an incoming Stripe webhook event.

    Handles:
    - payment_intent.succeeded  → marks booking as 'paid'
    - payment_intent.payment_failed → resets booking to 'unpaid'
    """
    try:
        event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except stripe.SignatureVerificationError:
        raise StripeError(400, "Invalid webhook signature")
    except ValueError:
        raise StripeError(400, "Invalid webhook payload")

    event_id = event["id"]

    # Idempotency check — skip events we've already processed.
    already_processed = (
        db.query(StripeWebhookEvent)
        .filter(StripeWebhookEvent.event_id == event_id)
        .first()
    )
    if already_processed:
        logger.info("Duplicate webhook event %s — skipping", event_id)
        return {"received": True}

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "payment_intent.succeeded":
        _on_payment_succeeded(data, db)
    elif event_type == "payment_intent.payment_failed":
        _on_payment_failed(data, db)
    elif event_type == "account.updated":
        _on_account_updated(data, db)
    else:
        logger.debug("Unhandled Stripe event type: %s", event_type)

    # Record the event so retries are ignored.
    db.add(StripeWebhookEvent(event_id=event_id, processed_at=datetime.now(timezone.utc)))
    db.commit()

    return {"received": True}


def _on_payment_succeeded(intent: dict, db: Session) -> None:
    booking_id = intent.get("metadata", {}).get("booking_id")
    if not booking_id:
        return

    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        logger.warning("payment_intent.succeeded: booking %s not found", booking_id)
        return

    booking.payment_status = "paid"
    booking.status = "payment_confirmed"
    booking.paid_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Booking %s marked as paid (intent %s)", booking_id, intent["id"])


def _on_payment_failed(intent: dict, db: Session) -> None:
    booking_id = intent.get("metadata", {}).get("booking_id")
    if not booking_id:
        return

    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        return

    booking.payment_status = "unpaid"
    db.commit()
    logger.warning("Payment failed for booking %s (intent %s)", booking_id, intent["id"])


def _on_account_updated(account: dict, db: Session) -> None:
    """Sync stripe_onboarding_complete when Stripe fires account.updated.

    Stripe sends this event whenever any field on the Connect account changes,
    including when the vendor finishes filling in KYC details. We check
    details_submitted so the vendor can accept payments without having to
    manually call the status endpoint.
    """
    stripe_account_id = account.get("id")
    if not stripe_account_id:
        return

    vendor = db.query(Vendor).filter(Vendor.stripe_account_id == stripe_account_id).first()
    if not vendor:
        logger.debug("account.updated: no vendor found for Stripe account %s", stripe_account_id)
        return

    complete = bool(account.get("details_submitted"))
    if complete != vendor.stripe_onboarding_complete:
        vendor.stripe_onboarding_complete = complete
        db.commit()
        logger.info(
            "Stripe onboarding %s for vendor %s (account %s)",
            "completed" if complete else "reverted",
            vendor.vendor_id,
            stripe_account_id,
        )


# ── Event confirmation & fund release ────────────────────────────────


def confirm_event(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    """Record that the customer or vendor has confirmed the event took place.

    When both parties have confirmed, funds are automatically transferred
    to the vendor's Stripe Connect account (minus the platform fee).
    """
    # Lock the row so two simultaneous confirms can't both trigger a transfer.
    booking = (
        db.query(Booking)
        .filter(Booking.booking_id == booking_id)
        .with_for_update()
        .first()
    )
    if not booking:
        raise StripeError(404, "Booking not found")

    # Derive role from identity — never trust a client-supplied flag.
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    is_vendor = vendor is not None and vendor.user_id == caller_user_id
    is_customer = booking.user_id == caller_user_id
    if not is_vendor and not is_customer:
        raise StripeError(403, "You are not a party to this booking")

    if booking.payment_status == "released":
        return {"message": "Funds have already been released for this booking."}

    if booking.payment_status != "paid":
        raise StripeError(400, "Cannot confirm an event that has not been paid for")

    now = datetime.now(timezone.utc)

    if is_vendor:
        if booking.vendor_confirmed_at:
            raise StripeError(400, "Vendor has already confirmed this event")
        booking.vendor_confirmed_at = now
    else:
        if booking.customer_confirmed_at:
            raise StripeError(400, "Customer has already confirmed this event")
        booking.customer_confirmed_at = now

    db.commit()

    # Release funds once both parties have confirmed
    if booking.customer_confirmed_at and booking.vendor_confirmed_at:
        _release_funds(booking, db)
        return {"message": "Event confirmed. Funds have been released to the vendor."}

    waiting_for = "the vendor" if not booking.vendor_confirmed_at else "the customer"
    return {"message": f"Confirmation recorded. Waiting for {waiting_for} to confirm."}


def _release_funds(booking: Booking, db: Session) -> None:
    """Transfer the vendor's share from the platform balance to their Connect account.

    Called automatically when both parties have confirmed the event.
    """
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    if not vendor or not vendor.stripe_account_id:
        raise StripeError(500, "Vendor Stripe account not found — cannot release funds")

    vendor_amount_cents = booking.amount_cents - booking.platform_fee_cents

    try:
        stripe.Transfer.create(
            amount=vendor_amount_cents,
            currency=booking.currency,
            destination=vendor.stripe_account_id,
            transfer_group=booking.booking_id,
            metadata={"booking_id": booking.booking_id},
            # Idempotency key ensures Stripe deduplicates this transfer even if
            # the request is retried after a network failure.
            idempotency_key=f"release_{booking.booking_id}",
        )
    except stripe.StripeError as e:
        raise StripeError(502, f"Stripe transfer failed: {e.user_message or str(e)}")

    booking.payment_status = "released"
    booking.funds_released_at = datetime.now(timezone.utc)
    db.commit()
    logger.info(
        "Released %d cents to vendor %s for booking %s",
        vendor_amount_cents,
        booking.vendor_id,
        booking.booking_id,
    )


# ── Refund ────────────────────────────────────────────────────────────

REFUND_WINDOW_HOURS = 24


def request_refund(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    """Issue a full refund if the customer cancels within 24 hours of
    the booking being confirmed (vendor approval timestamp).

    Raises StripeError 400 if outside the refund window or not eligible.
    """
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise StripeError(404, "Booking not found")

    if booking.user_id != caller_user_id:
        raise StripeError(403, "You are not the customer for this booking")

    if booking.payment_status not in ("paid", "processing"):
        raise StripeError(
            400,
            f"Booking is not eligible for a refund (payment status: '{booking.payment_status}')",
        )

    if booking.payment_status == "released":
        raise StripeError(400, "Funds have already been released to the vendor")

    # Check 24-hour refund window from when the vendor confirmed the booking
    if not booking.confirmed_at:
        raise StripeError(400, "Booking has no confirmation timestamp — cannot process refund")

    now = datetime.now(timezone.utc)
    confirmed_at = booking.confirmed_at
    # Make timezone-aware if stored as naive UTC
    if confirmed_at.tzinfo is None:
        confirmed_at = confirmed_at.replace(tzinfo=timezone.utc)

    if now - confirmed_at > timedelta(hours=REFUND_WINDOW_HOURS):
        raise StripeError(
            400,
            f"Refund window has closed. Refunds are only available within "
            f"{REFUND_WINDOW_HOURS} hours of booking confirmation.",
        )

    if not booking.payment_intent_id:
        raise StripeError(500, "No payment intent found for this booking")

    try:
        stripe.Refund.create(
            payment_intent=booking.payment_intent_id,
            reason="requested_by_customer",
        )
    except stripe.StripeError as e:
        raise StripeError(502, f"Stripe refund failed: {e.user_message or str(e)}")

    booking.payment_status = "refunded"
    db.commit()
    logger.info("Refund issued for booking %s", booking_id)

    return {"message": "Refund issued successfully. Funds will be returned within 5–10 business days."}


# ── Disputes ──────────────────────────────────────────────────────────


def raise_dispute(*, booking_id: str, caller_user_id: str, reason: str | None, db: Session) -> dict:
    """Mark a paid booking as disputed, freezing funds on the platform.

    Only the customer may raise a dispute, and only while payment_status is 'paid'
    (funds still held on the platform). Once disputed, auto-release is blocked
    until an admin resolves it.
    """
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise StripeError(404, "Booking not found")
    if booking.user_id != caller_user_id:
        raise StripeError(403, "Only the customer can raise a dispute")
    if booking.payment_status != "paid":
        raise StripeError(
            400,
            f"Disputes can only be raised while payment is held on the platform "
            f"(current status: '{booking.payment_status}'). "
            "If funds were already released, contact support directly."
        )

    booking.payment_status = "disputed"
    db.commit()
    logger.info("Dispute raised for booking %s by user %s", booking_id, caller_user_id)
    return {
        "message": "Dispute raised. Our team will review and resolve it within 3–5 business days.",
        "booking_id": booking_id,
        "payment_status": "disputed",
    }


def resolve_dispute(*, booking_id: str, resolution: str, db: Session) -> dict:
    """Resolve a disputed booking. Caller must be an admin (enforced at the router level).

    resolution must be one of:
    - 'refund_customer' — issue a full Stripe refund to the customer
    - 'release_vendor'  — transfer funds to the vendor as normal
    """
    if resolution not in ("refund_customer", "release_vendor"):
        raise StripeError(400, "resolution must be 'refund_customer' or 'release_vendor'")

    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise StripeError(404, "Booking not found")
    if booking.payment_status != "disputed":
        raise StripeError(400, f"Booking is not disputed (status: '{booking.payment_status}')")
    if not booking.payment_intent_id:
        raise StripeError(500, "No payment intent found for this booking")

    if resolution == "refund_customer":
        try:
            stripe.Refund.create(
                payment_intent=booking.payment_intent_id,
                reason="fraudulent",
            )
        except stripe.StripeError as e:
            raise StripeError(502, f"Stripe refund failed: {e.user_message or str(e)}")
        booking.payment_status = "refunded"
        db.commit()
        logger.info("Dispute resolved: refund issued for booking %s", booking_id)
        return {"message": "Dispute resolved. Customer has been refunded.", "payment_status": "refunded"}

    # release_vendor — transfer funds to vendor
    _release_funds(booking, db)
    logger.info("Dispute resolved: funds released to vendor for booking %s", booking_id)
    return {"message": "Dispute resolved. Funds have been released to the vendor.", "payment_status": "released"}
