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


def _sv(obj, key, default=None):
    """Read ``key`` from a Stripe object (or plain dict), tolerating absence.

    stripe-python's StripeObject is not dict-subclassed (v5+), so ``.get()`` is
    unavailable and bracket access raises KeyError for missing keys. This wrapper
    restores dict-style ``.get`` semantics across StripeObjects and plain dicts.
    """
    if obj is None:
        return default
    try:
        return obj[key]
    except (KeyError, TypeError):
        return default


# ── Vendor onboarding ─────────────────────────────────────────────────


def create_vendor_onboarding_url(*, vendor_id: str, caller_user_id: str, db: Session, base_url: str | None = None) -> dict:
    """Create (or reuse) a Stripe Express Connect account for the vendor
    and return a one-time hosted onboarding URL.

    If the vendor already has a stripe_account_id, a fresh Account Link is
    generated for the same account (handles the case where the vendor
    didn't finish onboarding the first time).

    ``base_url`` (the API's own public base) is used for the return/refresh
    pages so they resolve to the backend-served landing pages regardless of
    how FRONTEND_URL is configured.
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

        link_base = (base_url or FRONTEND_URL).rstrip("/")
        account_link = stripe.AccountLink.create(
            account=vendor.stripe_account_id,
            refresh_url=f"{link_base}/vendor/stripe-onboard/refresh?vendor_id={vendor_id}",
            return_url=f"{link_base}/vendor/stripe-onboard/return?vendor_id={vendor_id}",
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


# ── Earnings ──────────────────────────────────────────────────────────


def get_vendor_earnings(*, vendor_id: str, caller_user_id: str, db: Session) -> dict:
    """Summarize a vendor's money: released payouts, funds held in escrow,
    upcoming (approved but not yet paid) bookings, and per-booking history.

    Built entirely from booking payment fields — no Stripe API calls — so it's
    fast and works even before onboarding completes. Net = amount − platform fee.
    """
    from app.db.models import Bundle

    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise StripeError(404, "Vendor not found")
    if vendor.user_id != caller_user_id:
        raise StripeError(403, "You are not authorised to view this vendor's earnings")

    bookings = db.query(Booking).filter(Booking.vendor_id == vendor_id).all()

    def net_cents(b: Booking) -> int:
        amount = b.amount_cents or 0
        fee = b.platform_fee_cents or 0
        return max(amount - fee, 0)

    released = [b for b in bookings if b.payment_status == "released"]
    in_escrow = [b for b in bookings if b.payment_status in ("paid", "processing")]
    disputed = [b for b in bookings if b.payment_status == "disputed"]
    refunded = [b for b in bookings if b.payment_status == "refunded"]
    # Approved but unpaid — the client still has to pay; estimate from the
    # booking amount when set, else the service's listed price.
    upcoming = [b for b in bookings if b.status == "approved" and b.payment_status == "unpaid"]

    from app.services.booking_service import resolve_total_cents

    def upcoming_cents(b: Booking) -> int:
        service = db.query(Service).filter(Service.service_id == b.service_id).first()
        total = resolve_total_cents(b, service)
        if total is not None:
            return total
        # Rate-priced with an unknown quantity — floor the projection at the
        # listed rate (the real total, once known, can only be higher).
        return round((service.price if service else 0) * 100)

    # Per-booking history for everything with payment activity, newest first.
    history_bookings = [b for b in bookings if b.payment_status != "unpaid"]
    bundle_ids = {b.bundle_id for b in history_bookings if b.bundle_id}
    bundles = {
        bu.bundle_id: bu
        for bu in db.query(Bundle).filter(Bundle.bundle_id.in_(bundle_ids)).all()
    } if bundle_ids else {}
    client_ids = {b.user_id for b in history_bookings}
    clients = {
        u.user_id: u
        for u in db.query(User).filter(User.user_id.in_(client_ids)).all()
    } if client_ids else {}

    def entry(b: Booking) -> dict:
        bundle = bundles.get(b.bundle_id) if b.bundle_id else None
        client = clients.get(b.user_id)
        return {
            "booking_id": b.booking_id,
            "event_name": (bundle.event_name or bundle.name) if bundle else None,
            "client_name": f"{client.f_name} {client.l_name}" if client else None,
            "date_iso": b.date_iso,
            "amount_cents": b.amount_cents or 0,
            "platform_fee_cents": b.platform_fee_cents or 0,
            "net_cents": net_cents(b),
            "payment_status": b.payment_status,
            "paid_at": b.paid_at.isoformat() if b.paid_at else None,
            "funds_released_at": b.funds_released_at.isoformat() if b.funds_released_at else None,
        }

    history = sorted(
        (entry(b) for b in history_bookings),
        key=lambda e: e["paid_at"] or "",
        reverse=True,
    )

    return {
        "vendor_id": vendor_id,
        "total_released_cents": sum(net_cents(b) for b in released),
        "in_escrow_cents": sum(net_cents(b) for b in in_escrow),
        "upcoming_cents": sum(upcoming_cents(b) for b in upcoming),
        "upcoming_count": len(upcoming),
        "disputed_cents": sum(net_cents(b) for b in disputed),
        "refunded_cents": sum((b.amount_cents or 0) for b in refunded),
        "platform_fees_cents": sum((b.platform_fee_cents or 0) for b in released),
        "history": history,
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

    # Resolve the total via the single source of truth: stored amount, else a
    # recomputed rate x quantity estimate, else the flat price for event-priced
    # services. None => rate-priced (per person/day/hour) with an unknown
    # quantity — refuse rather than charge the bare per-unit rate as a total.
    from app.services.booking_service import resolve_total_cents, pending_quantity_reason
    amount_cents = resolve_total_cents(booking, service)
    if amount_cents is None:
        raise StripeError(
            400,
            f"This booking is priced per {service.price_unit or 'unit'}. Add "
            f"{pending_quantity_reason(service)} before paying so we can total it correctly.",
        )
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


# ── Hosted Checkout Session ───────────────────────────────────────────


def create_checkout_session(*, booking_id: str, caller_user_id: str, base_url: str, db: Session) -> dict:
    """Create a Stripe-hosted Checkout Session for an approved booking.

    Mirrors create_payment_intent's escrow model — the charge lands in the
    Desiconnect platform balance and is transferred to the vendor later, once
    both parties confirm the event (see confirm_event / _release_funds). The
    underlying PaymentIntent carries the same metadata + transfer_group, so the
    existing webhook handling works unchanged.

    Returns a hosted ``checkout_url`` the app opens in the browser.
    payment_status is left untouched until the webhook confirms payment, so an
    abandoned session can simply be retried.
    """
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise StripeError(404, "Booking not found")
    if booking.user_id != caller_user_id:
        raise StripeError(403, "You are not the customer for this booking")
    if booking.status != "approved":
        raise StripeError(400, "Payment can only be initiated for approved bookings")
    if booking.payment_status not in ("unpaid", "processing"):
        raise StripeError(400, f"Booking payment is already '{booking.payment_status}'")

    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    if not service:
        raise StripeError(404, "Service not found")

    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    if not vendor or not vendor.stripe_onboarding_complete:
        raise StripeError(
            400,
            "This vendor has not completed Stripe onboarding and cannot accept payments yet.",
        )

    # Resolve the total via the single source of truth (see create_payment_intent).
    # None => rate-priced with an unknown quantity — refuse rather than charge the
    # bare per-unit rate as a total.
    from app.services.booking_service import resolve_total_cents, pending_quantity_reason
    amount_cents = resolve_total_cents(booking, service)
    if amount_cents is None:
        raise StripeError(
            400,
            f"This booking is priced per {service.price_unit or 'unit'}. Add "
            f"{pending_quantity_reason(service)} before paying so we can total it correctly.",
        )
    platform_fee_cents = round(amount_cents * PLATFORM_FEE_PERCENT / 100)

    base = base_url.rstrip("/")
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "price_data": {
                        "currency": booking.currency,
                        "product_data": {"name": service.name or "Booking"},
                        "unit_amount": amount_cents,
                    },
                    "quantity": 1,
                }
            ],
            # Propagate to the underlying PaymentIntent so the existing
            # payment_intent.succeeded webhook + Transfer flow work unchanged.
            payment_intent_data={
                "transfer_group": booking_id,
                "metadata": {
                    "booking_id": booking_id,
                    "vendor_id": booking.vendor_id,
                    "user_id": booking.user_id,
                },
            },
            metadata={"booking_id": booking_id},
            success_url=f"{base}/payment-complete?booking_id={booking_id}&status=success",
            cancel_url=f"{base}/payment-complete?booking_id={booking_id}&status=cancel",
        )
    except stripe.StripeError as e:
        raise StripeError(502, f"Stripe error: {e.user_message or str(e)}")

    # Persist amounts so fund-release / refund can compute the vendor's share, and
    # the session id so we can reconcile payment status directly with Stripe on
    # return from checkout (see sync_booking_payment). payment_status stays as-is
    # until a completed payment is confirmed (webhook or reconcile).
    booking.amount_cents = amount_cents
    booking.platform_fee_cents = platform_fee_cents
    booking.checkout_session_id = session.id
    db.commit()

    logger.info(
        "Created Checkout Session %s for booking %s (%d cents)", session.id, booking_id, amount_cents
    )

    return {"checkout_url": session.url}


def sync_booking_payment(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    """Reconcile a booking's payment status directly with Stripe.

    A safety net for when the payment_intent.succeeded webhook is delayed or
    misconfigured: the app calls this when the customer returns from hosted
    Checkout, and we ask Stripe whether the charge actually completed rather than
    waiting on the webhook. Idempotent — a harmless no-op once the webhook (or a
    previous sync) has already marked the booking paid.
    """
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise StripeError(404, "Booking not found")
    if booking.user_id != caller_user_id:
        raise StripeError(403, "You are not the customer for this booking")

    # Terminal / already-synced states need no Stripe round-trip.
    if booking.payment_status in ("paid", "released", "refunded", "disputed"):
        return {"booking_id": booking_id, "payment_status": booking.payment_status, "updated": False}

    paid = False
    intent_id: str | None = None
    try:
        if booking.checkout_session_id:
            session = stripe.checkout.Session.retrieve(booking.checkout_session_id)
            paid = _sv(session, "payment_status") == "paid"
            # `payment_intent` is a bare id string when the session isn't expanded.
            intent_id = _sv(session, "payment_intent")
        elif booking.payment_intent_id:
            intent = stripe.PaymentIntent.retrieve(booking.payment_intent_id)
            paid = _sv(intent, "status") == "succeeded"
            intent_id = _sv(intent, "id")
    except stripe.StripeError as e:
        raise StripeError(502, f"Stripe error: {e.user_message or str(e)}")

    updated = _mark_booking_paid(booking, intent_id, db) if paid else False
    return {"booking_id": booking_id, "payment_status": booking.payment_status, "updated": updated}


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


def _mark_booking_paid(booking: Booking, payment_intent_id: str | None, db: Session) -> bool:
    """Idempotently transition a booking to 'paid'.

    Shared by the payment_intent.succeeded webhook and the reconcile-on-return
    path (sync_booking_payment) so both apply the exact same state change.
    Returns True if this call actually moved the booking to 'paid'.
    """
    # Never walk back a further-along state (funds released / refunded / disputed).
    if booking.payment_status in ("released", "refunded", "disputed"):
        return False

    # Already paid — just backfill the PaymentIntent id if we now have one
    # (hosted Checkout doesn't set it at session-creation time; refunds need it).
    if booking.payment_status == "paid":
        if payment_intent_id and not booking.payment_intent_id:
            booking.payment_intent_id = payment_intent_id
            db.commit()
        return False

    booking.payment_status = "paid"
    booking.status = "payment_confirmed"
    booking.paid_at = datetime.now(timezone.utc)
    if payment_intent_id and not booking.payment_intent_id:
        booking.payment_intent_id = payment_intent_id
    db.commit()
    logger.info("Booking %s marked as paid (intent %s)", booking.booking_id, payment_intent_id)
    return True


def _on_payment_succeeded(intent: dict, db: Session) -> None:
    booking_id = _sv(_sv(intent, "metadata"), "booking_id")
    if not booking_id:
        return

    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        logger.warning("payment_intent.succeeded: booking %s not found", booking_id)
        return

    _mark_booking_paid(booking, _sv(intent, "id"), db)


def _on_payment_failed(intent: dict, db: Session) -> None:
    booking_id = _sv(_sv(intent, "metadata"), "booking_id")
    if not booking_id:
        return

    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        return

    booking.payment_status = "unpaid"
    db.commit()
    logger.warning("Payment failed for booking %s (intent %s)", booking_id, _sv(intent, "id"))


def _on_account_updated(account: dict, db: Session) -> None:
    """Sync stripe_onboarding_complete when Stripe fires account.updated.

    Stripe sends this event whenever any field on the Connect account changes,
    including when the vendor finishes filling in KYC details. We check
    details_submitted so the vendor can accept payments without having to
    manually call the status endpoint.
    """
    stripe_account_id = _sv(account, "id")
    if not stripe_account_id:
        return

    vendor = db.query(Vendor).filter(Vendor.stripe_account_id == stripe_account_id).first()
    if not vendor:
        logger.debug("account.updated: no vendor found for Stripe account %s", stripe_account_id)
        return

    complete = bool(_sv(account, "details_submitted"))
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

    # Escrow is held until the event has taken place — neither party can confirm
    # (and trigger release) before the event date. A TBD date isn't confirmable
    # until a real date is set. (Admin dispute-resolution release bypasses this.)
    from app.services.booking_service import event_confirmable_date
    ok, msg = event_confirmable_date(booking)
    if not ok:
        raise StripeError(400, msg)

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
    """Issue a full refund if the customer cancels within 24 hours of paying.

    The window runs from when the customer actually paid (``paid_at``), NOT from
    vendor approval — otherwise a client who pays a day or more after approval
    would have little or no window left the moment they pay.

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

    # Check the 24-hour refund window from when the customer actually paid.
    if not booking.paid_at:
        raise StripeError(400, "Booking payment has not completed — cannot process refund")

    now = datetime.now(timezone.utc)
    paid_at = booking.paid_at
    # Make timezone-aware if stored as naive UTC
    if paid_at.tzinfo is None:
        paid_at = paid_at.replace(tzinfo=timezone.utc)

    if now - paid_at > timedelta(hours=REFUND_WINDOW_HOURS):
        raise StripeError(
            400,
            f"Refund window has closed. Refunds are only available within "
            f"{REFUND_WINDOW_HOURS} hours of payment.",
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
    # If this was the venue, re-sync so its anchor clears and the other vendors
    # stop being able to check in against a venue that's now refunded.
    try:
        from app.services.booking_service import sync_event_venue
        sync_event_venue(booking.bundle_id, db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("request_refund: venue re-sync failed for %s: %s", booking_id, exc)
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
        try:
            from app.services.booking_service import sync_event_venue
            sync_event_venue(booking.bundle_id, db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve_dispute: venue re-sync failed for %s: %s", booking_id, exc)
        db.commit()
        logger.info("Dispute resolved: refund issued for booking %s", booking_id)
        return {"message": "Dispute resolved. Customer has been refunded.", "payment_status": "refunded"}

    # release_vendor — transfer funds to vendor
    _release_funds(booking, db)
    logger.info("Dispute resolved: funds released to vendor for booking %s", booking_id)
    return {"message": "Dispute resolved. Funds have been released to the vendor.", "payment_status": "released"}
