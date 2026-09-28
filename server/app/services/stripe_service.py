"""Business logic for Stripe Connect vendor onboarding and payments."""

import logging
from datetime import datetime, timedelta, timezone

import stripe

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, FRONTEND_URL, PLATFORM_FEE_PERCENT
from app.db.models import Vendor, Booking, Service, StripeWebhookEvent, User
from app.models.schemas import PaymentStatus

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


def _onboarding_complete(account) -> bool:
    """Whether Stripe will actually let this vendor be paid.

    ``details_submitted`` alone only says the vendor reached the end of the
    onboarding form. It stays true while Stripe is still waiting on something it
    asked for and never got — an ID number, a document — and an account in that
    state has ``payouts_enabled`` false. Escrow released to it lands in a Stripe
    balance the vendor can see and cannot withdraw, which is a worse place for
    their money than escrow: at least escrow has a refund route out.

    So the flag this product gates payment on has to mean "payable", not "filled
    the form in". A vendor who lapses back into ``requirements.past_due`` stops
    being bookable until they fix it, which is the honest moment to find out.

    The capability checked is ``transfers``, not ``charges_enabled``. This
    integration charges on the platform account and moves the vendor's share
    with a separate Transfer (see _release_funds); ``charges_enabled`` governs
    charges a connected account makes for itself, which is not something any
    vendor here ever does.
    """
    caps = _sv(account, "capabilities")
    return bool(
        _sv(account, "details_submitted")
        and _sv(account, "payouts_enabled")
        and _sv(caps, "transfers") == "active"
    )


def _payability(account) -> dict:
    """What Stripe is waiting on, so the vendor can be told instead of guessing.

    Without this the product knew only that a vendor wasn't payable, and every
    screen said the same unhelpful thing — "payment setup incomplete" — to
    someone who had completed it months ago and was now missing one field they
    were never named. The fix for that is one Stripe re-asked for, so it is the
    one thing worth carrying back.

    ``currently_due`` and ``past_due`` overlap (past_due is the subset already
    late) and are merged: to the vendor they are one list of things to go and
    do. ``pending_verification`` is deliberately separate — Stripe is checking
    something it already has, and there is nothing for the vendor to do about
    it, so a screen that demanded action would be lying.
    """
    req = _sv(account, "requirements")
    due = list(_sv(req, "currently_due") or []) + list(_sv(req, "past_due") or [])
    return {
        "details_submitted": bool(_sv(account, "details_submitted")),
        "payouts_enabled": bool(_sv(account, "payouts_enabled")),
        "disabled_reason": _sv(req, "disabled_reason"),
        "requirements_due": sorted(set(due)),
        "pending_verification": bool(_sv(req, "pending_verification")),
    }


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

    # Same keys either way. A caller distinguishing "never started" from "started
    # and stalled" should read stripe_account_id, not the shape of the response.
    if not vendor.stripe_account_id:
        return {
            "stripe_account_id": None,
            "stripe_onboarding_complete": False,
            **_payability(None),
        }

    try:
        account = stripe.Account.retrieve(vendor.stripe_account_id)
        complete = _onboarding_complete(account)

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
        **_payability(account),
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
        # A cancelled booking's vendor share came from the cancellation split,
        # not the ordinary platform fee — amount_cents minus platform_fee_cents
        # would be the wrong number (and platform_fee_cents is usually unset
        # for one anyway, since it's never charged past the send/accept step).
        if b.payment_status == PaymentStatus.CANCELLED.value:
            return b.vendor_cancellation_cents or 0
        amount = b.amount_cents or 0
        fee = b.platform_fee_cents or 0
        return max(amount - fee, 0)

    released = [b for b in bookings if b.payment_status == PaymentStatus.RELEASED.value]
    cancelled = [b for b in bookings if b.payment_status == PaymentStatus.CANCELLED.value]
    in_escrow = [b for b in bookings if b.payment_status in (PaymentStatus.PAID.value, PaymentStatus.PROCESSING.value)]
    disputed = [b for b in bookings if b.payment_status == PaymentStatus.DISPUTED.value]
    refunded = [b for b in bookings if b.payment_status == PaymentStatus.REFUNDED.value]
    # Approved but unpaid — the client still has to pay; estimate from the
    # booking amount when set, else the service's listed price.
    upcoming = [b for b in bookings if b.status == "approved" and b.payment_status == PaymentStatus.UNPAID.value]
    # Manual track — self-reported, never touched by Jorna, so kept out of
    # total_released_cents entirely rather than blended in as if verified.
    self_reported = [b for b in bookings if b.payment_status == PaymentStatus.CONFIRMED_PAID.value]
    awaiting_confirmation = [b for b in bookings if b.payment_status == PaymentStatus.MARKED_PAID.value]

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
    history_bookings = [b for b in bookings if b.payment_status != PaymentStatus.UNPAID.value]
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
        # released and cancelled are both "paid to the vendor," just via
        # different mechanisms (an event-completion Transfer vs. a
        # cancellation-split Transfer) — net_cents already branches on which.
        "total_released_cents": sum(net_cents(b) for b in released + cancelled),
        "in_escrow_cents": sum(net_cents(b) for b in in_escrow),
        "upcoming_cents": sum(upcoming_cents(b) for b in upcoming),
        "upcoming_count": len(upcoming),
        "disputed_cents": sum(net_cents(b) for b in disputed),
        "refunded_cents": sum((b.amount_cents or 0) for b in refunded),
        "platform_fees_cents": sum((b.platform_fee_cents or 0) for b in released),
        # Manual track — paid directly, confirmed by the vendor. Self-reported,
        # so kept separate from every Stripe-verified bucket above.
        "self_reported_cents": sum(net_cents(b) for b in self_reported),
        "self_reported_pending_cents": sum(net_cents(b) for b in awaiting_confirmation),
        "self_reported_pending_count": len(awaiting_confirmation),
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

    if booking.payment_status != PaymentStatus.UNPAID.value:
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
    booking.payment_status = PaymentStatus.PROCESSING.value
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
    if booking.payment_status not in (PaymentStatus.UNPAID.value, PaymentStatus.PROCESSING.value):
        raise StripeError(400, f"Booking payment is already '{booking.payment_status}'")

    # Re-check availability at payment time: another booking for this vendor on an
    # overlapping date may have been approved/paid since this one was approved.
    # Refuse rather than take escrow for a vendor who's already committed elsewhere.
    from app.services.booking_service import vendor_has_conflicting_booking
    if vendor_has_conflicting_booking(
        vendor_id=booking.vendor_id,
        date_iso=booking.date_iso,
        date_end=booking.date_end,
        time_start=booking.time_start,
        time_end=booking.time_end,
        db=db,
        exclude_booking_id=booking.booking_id,
    ):
        raise StripeError(
            409,
            "This vendor is no longer available on your event date — another "
            "booking for that day was confirmed first.",
        )

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
    if booking.payment_status in (PaymentStatus.PAID.value, PaymentStatus.RELEASED.value, PaymentStatus.REFUNDED.value, PaymentStatus.DISPUTED.value):
        return {"booking_id": booking_id, "payment_status": booking.payment_status, "updated": False}

    paid = False
    intent_id: str | None = None
    try:
        if booking.checkout_session_id:
            session = stripe.checkout.Session.retrieve(booking.checkout_session_id)
            # This is Stripe's own Checkout Session.payment_status vocabulary
            # ("paid"/"unpaid"/"no_payment_required"), not our PaymentStatus —
            # they only happen to share a spelling for "paid".
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
    if booking.payment_status in (PaymentStatus.RELEASED.value, PaymentStatus.REFUNDED.value, PaymentStatus.DISPUTED.value):
        return False

    # The booking was cancelled while this payment was in flight.
    #
    # create_checkout_session deliberately leaves payment_status alone so an
    # abandoned session can be retried — which means a client on Stripe's page
    # still reads as unpaid, and a vendor cancelling in that window passes the
    # "no money has moved" check honestly. Then the webhook arrives.
    #
    # Taking the money and marking a rejected booking paid is the one outcome
    # nobody wants, so it is returned rather than recorded. Best-effort: if the
    # refund itself fails, the booking is still left unpaid and the money shows
    # up as stranded on the client's plan, which is visible and recoverable.
    if booking.status == "rejected":
        logger.warning(
            "Payment landed on cancelled booking %s — refunding", booking.booking_id
        )
        intent = payment_intent_id or booking.payment_intent_id
        if intent:
            try:
                stripe.Refund.create(payment_intent=intent, reason="requested_by_customer")
                booking.payment_status = PaymentStatus.REFUNDED.value
                booking.payment_intent_id = intent
                db.commit()
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Auto-refund failed for cancelled booking %s: %s",
                    booking.booking_id, exc,
                )
        return False

    # Already paid — just backfill the PaymentIntent id if we now have one
    # (hosted Checkout doesn't set it at session-creation time; refunds need it).
    if booking.payment_status == PaymentStatus.PAID.value:
        if payment_intent_id and not booking.payment_intent_id:
            booking.payment_intent_id = payment_intent_id
            db.commit()
        return False

    booking.payment_status = PaymentStatus.PAID.value
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

    booking.payment_status = PaymentStatus.UNPAID.value
    db.commit()
    logger.warning("Payment failed for booking %s (intent %s)", booking_id, _sv(intent, "id"))


def _on_account_updated(account: dict, db: Session) -> None:
    """Sync stripe_onboarding_complete when Stripe fires account.updated.

    Stripe sends this event whenever any field on the Connect account changes,
    including when the vendor finishes filling in KYC details — so the vendor
    can accept payments without having to manually call the status endpoint.

    Both directions matter, which is why this reads _onboarding_complete rather
    than details_submitted: the same event fires when Stripe *withdraws* a
    payout capability it had granted, and that is precisely the transition a
    vendor should stop being bookable on.
    """
    stripe_account_id = _sv(account, "id")
    if not stripe_account_id:
        return

    vendor = db.query(Vendor).filter(Vendor.stripe_account_id == stripe_account_id).first()
    if not vendor:
        logger.debug("account.updated: no vendor found for Stripe account %s", stripe_account_id)
        return

    complete = _onboarding_complete(account)
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

    if booking.payment_status == PaymentStatus.RELEASED.value:
        return {
            "message": "Funds have already been released for this booking.",
            "funds_released": True,
        }

    if booking.payment_status != PaymentStatus.PAID.value:
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
        try:
            _release_funds(booking, db)
        except StripeError as exc:
            # The confirmation above is committed, and true. The transfer is a
            # separate thing that fails for reasons with nothing to do with the
            # person who pressed the button — an insufficient platform balance,
            # a vendor whose payouts Stripe has since disabled.
            #
            # Raising here reported the whole call as failed, so a client was
            # told their confirmation hadn't registered when it had, and their
            # one obvious move — press it again — was met with "already
            # confirmed". Nothing on the screen could then explain where the
            # money was, and the card went on naming the vendor as the holdup.
            #
            # So the confirmation is reported as what it is, and the payout as
            # owed. auto_release_due sweeps exactly this state — both parties
            # confirmed, still 'paid' — with no waiting period, which is what
            # makes that a promise rather than a hope.
            logger.warning(
                "confirm_event: release failed for booking %s, left to the sweep: %s",
                booking.booking_id,
                exc.detail,
            )
            return {
                "message": (
                    "Event confirmed by both of you. The payout to the vendor "
                    "didn't go through yet — we'll keep retrying it."
                ),
                "funds_released": False,
            }
        return {
            "message": "Event confirmed. Funds have been released to the vendor.",
            "funds_released": True,
        }

    waiting_for = "the vendor" if not booking.vendor_confirmed_at else "the customer"
    return {
        "message": f"Confirmation recorded. Waiting for {waiting_for} to confirm.",
        "funds_released": False,
    }


def _release_funds(booking: Booking, db: Session) -> None:
    """Transfer the vendor's share from the platform balance to their Connect account.

    Called automatically when both parties have confirmed the event.
    """
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    if not vendor or not vendor.stripe_account_id:
        raise StripeError(500, "Vendor Stripe account not found — cannot release funds")

    vendor_amount_cents = booking.amount_cents - booking.platform_fee_cents

    # A transfer already filed against this booking means an earlier attempt
    # reached Stripe and we never learned the outcome. Adopt it instead of
    # sending a second one. This — not the idempotency key — is what makes a
    # retry safe at any distance in time, and it's the guard that has to exist
    # before the key below is allowed to change between attempts.
    existing = None
    try:
        found = stripe.Transfer.list(transfer_group=booking.booking_id, limit=1)
        existing = found.data[0] if found.data else None
    except stripe.StripeError as e:
        raise StripeError(502, f"Stripe transfer failed: {e.user_message or str(e)}")

    if existing is None:
        try:
            stripe.Transfer.create(
                amount=vendor_amount_cents,
                currency=booking.currency,
                destination=vendor.stripe_account_id,
                transfer_group=booking.booking_id,
                metadata={"booking_id": booking.booking_id},
                # Deduplicates an immediate retry — a lost response, two sweeps
                # overlapping — without poisoning the booking forever.
                #
                # The key used to be the booking id alone, and Stripe caches a
                # key's outcome for 24 hours *including failures*. So the first
                # transfer that failed for a reason nothing to do with this
                # booking — an insufficient platform balance, a vendor Stripe
                # had stopped paying — took the booking's only key with it, and
                # every retry for the next 24 hours replayed that error instead
                # of trying. auto_release_due promises repair with no waiting
                # period; against a poisoned key it swept a booking it could
                # never fix, logged the same stale refusal each time, and the
                # money sat there while the balance that caused it was long
                # since topped up.
                idempotency_key=(
                    f"release_{booking.booking_id}_"
                    f"{datetime.now(timezone.utc):%Y%m%d%H}"
                ),
            )
        except stripe.StripeError as e:
            raise StripeError(502, f"Stripe transfer failed: {e.user_message or str(e)}")
    else:
        logger.warning(
            "Booking %s already had transfer %s — adopting it rather than sending another",
            booking.booking_id,
            existing["id"],
        )

    booking.payment_status = PaymentStatus.RELEASED.value
    booking.funds_released_at = datetime.now(timezone.utc)
    db.commit()
    logger.info(
        "Released %d cents to vendor %s for booking %s",
        vendor_amount_cents,
        booking.vendor_id,
        booking.booking_id,
    )


# ── Automatic release ─────────────────────────────────────────────────

# How long after the event a vendor waits on a silent client before the money
# moves anyway.
AUTO_RELEASE_DAYS = 7


def auto_release_due(*, db: Session, now: datetime | None = None) -> dict:
    """Release escrow that is owed but still sitting on the platform.

    Two kinds. Bookings both parties have confirmed, where the payout never
    went through — repair work, since release is supposed to fire on the second
    confirmation. And bookings the client has left unanswered since the event.

    Release needs both parties to confirm, which means a client who simply
    stops opening the app holds a vendor's money indefinitely. The vendor did
    the work, the event has happened, and there was no route to being paid that
    didn't go through an admin. After a week, silence reads as assent.

    A vendor's own confirmation is still required, and deliberately: it is their
    statement that they turned up, and their GPS check-in makes it for them.
    Without it this would pay a vendor who never claimed to have delivered, to a
    client who merely wasn't looking — a worse failure than the one being fixed,
    and the only one of the two that can't be undone.

    Anything the client HAS said stops it. A dispute freezes the booking, as
    raise_dispute has always said it would; a refund has already taken the money
    back. Both leave payment_status somewhere other than 'paid', which is the
    only status this touches. An event with no real date never qualifies — 'TBD'
    sorts above any ISO date, so the comparison excludes it.

    Returns a summary rather than raising: one vendor's missing Stripe account
    must not stop the rest of the sweep.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=AUTO_RELEASE_DAYS)).date().isoformat()

    # Dates are stored as ISO strings, which compare correctly as text.
    last_day = func.coalesce(Booking.date_end, Booking.date_iso)

    # Both parties confirmed and the money still here. Release is meant to
    # happen the instant the second confirmation lands, so this is only ever
    # repair work — a Stripe call that failed, a confirmation backfilled after
    # the fact — but a booking in that state will otherwise sit forever, since
    # nothing else looks at it again. No waiting period: there is nothing left
    # to wait for.
    settled = (
        db.query(Booking)
        .filter(
            Booking.payment_status == PaymentStatus.PAID.value,
            Booking.vendor_confirmed_at.isnot(None),
            Booking.customer_confirmed_at.isnot(None),
        )
        .all()
    )

    # Confirmed by the vendor, unanswered by the client, and a week past the
    # event: silence taken as assent.
    unanswered = (
        db.query(Booking)
        .filter(
            Booking.payment_status == PaymentStatus.PAID.value,
            Booking.vendor_confirmed_at.isnot(None),
            Booking.customer_confirmed_at.is_(None),
            last_day.isnot(None),
            last_day <= cutoff,
        )
        .all()
    )

    due = settled + unanswered

    released, failed = [], []
    for booking in due:
        try:
            # customer_confirmed_at is deliberately left null. The client didn't
            # confirm, and writing a timestamp saying they did would put a
            # falsehood in the record every support question later reads. A
            # released booking with no customer confirmation is an auto-release,
            # legibly.
            _release_funds(booking, db)
            released.append(booking.booking_id)
        except Exception as exc:
            db.rollback()
            failed.append(booking.booking_id)
            logger.warning("Auto-release failed for booking %s: %s", booking.booking_id, exc)

    if released or failed:
        logger.info(
            "Auto-release sweep: %d released, %d failed (cutoff %s)",
            len(released), len(failed), cutoff,
        )
    return {"released": released, "failed": failed, "cutoff": cutoff}


# ── Cancellation & refund ─────────────────────────────────────────────

# How long after the vendor accepts a client keeps an unconditional full
# refund. After this, cancelling stops being about changing your mind and
# starts costing the vendor a held date — see cancellation_split.
GRACE_HOURS = 24


def _parse_event_date(value: str | None):
    """A booking's date_iso/date_end, parsed loosely. None for TBD/unparseable —
    an unscheduled event never triggers the ramp; a cancellation on one is
    always still within the grace-or-better outcome (see cancellation_split)."""
    if not value:
        return None
    s = value.strip()
    if not s or s.upper() == "TBD":
        return None
    from datetime import date as _date
    try:
        return _date.fromisoformat(s[:10])
    except ValueError:
        return None


def cancellation_split(booking: Booking, now: datetime) -> dict:
    """What cancelling this booking right now would pay out.

    Full refund to the client for GRACE_HOURS after the vendor accepts. After
    that, up to the day before the event, the client gets nothing back and the
    payment instead splits between the platform and the vendor — 99% platform
    / 1% vendor right after the grace period, sliding linearly to 1% / 99% by
    the day before the event. The closer to the event, the more the vendor is
    protected, since they turned down other work to hold the date.

    A booking accepted too close to the event for the ramp to have room to run
    (grace end lands on or after "the day before") jumps straight to the 99%
    vendor / 1% platform end the moment grace closes, rather than trying to
    interpolate over a negative or zero-length window.

    Returns cents; has no opinion on eligibility (a released/refunded/disputed
    booking, or one whose event has passed) — that's the caller's gate.
    """
    amount = booking.amount_cents or 0
    confirmed_at = booking.confirmed_at
    if confirmed_at is not None and confirmed_at.tzinfo is None:
        confirmed_at = confirmed_at.replace(tzinfo=timezone.utc)

    # Never accepted — nothing has started the grace clock, so there is
    # nothing to protect the vendor from yet.
    if confirmed_at is None:
        return {"refund_to_client_cents": amount, "vendor_cents": 0, "platform_cents": 0}

    t0 = confirmed_at + timedelta(hours=GRACE_HOURS)
    if now < t0:
        return {"refund_to_client_cents": amount, "vendor_cents": 0, "platform_cents": 0}

    event_date = _parse_event_date(booking.date_iso)
    t1 = (
        datetime.combine(event_date, datetime.min.time(), tzinfo=timezone.utc)
        - timedelta(days=1)
        if event_date
        else None
    )
    ratio = 1.0 if (t1 is None or t1 <= t0) else min(max((now - t0) / (t1 - t0), 0.0), 1.0)

    vendor_pct = 1 + 98 * ratio
    vendor_cents = round(amount * vendor_pct / 100)
    return {
        "refund_to_client_cents": 0,
        "vendor_cents": vendor_cents,
        "platform_cents": amount - vendor_cents,
    }


def cancellation_preview(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    """What cancel_booking would pay out right now — for the UI's eligibility
    countdown, so it reads the same numbers the real action would use rather
    than reimplementing the ramp in JS. Same shape ``_booking_summary``
    embeds inline as ``refund_preview`` — this is the standalone version for
    a caller that only has a booking id."""
    from app.services.bundle_service import _refund_preview

    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise StripeError(404, "Booking not found")
    if booking.user_id != caller_user_id:
        raise StripeError(403, "You are not the customer for this booking")

    if booking.payment_status != PaymentStatus.PAID.value:
        # Nothing held — either it's a manual-track booking (never reaches
        # "paid") or a Stripe one that hasn't been charged yet. Either way
        # there's no refund ramp to preview.
        return None
    return _refund_preview(booking)


def _finish_cancellation(
    *, booking: Booking, caller_user_id: str, db: Session,
    refund_cents: int, vendor_cents: int,
) -> dict:
    """Shared tail for cancel_booking's manual and Stripe branches: flip the
    status, best-effort calendar/chat bookkeeping, and the response payload.
    Callers have already decided refund_cents/vendor_cents (both 0 for a
    manual booking) and set payment_status themselves where it needs to
    change."""
    from app.models.schemas import BookingStatus

    now = datetime.now(timezone.utc)
    booking.status = BookingStatus.REJECTED.value
    booking.cancelled_at = now
    booking.refund_cents = refund_cents
    booking.vendor_cancellation_cents = vendor_cents

    try:
        from app.services.booking_service import sync_event_venue
        sync_event_venue(booking.bundle_id, db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cancel_booking: venue re-sync failed for %s: %s", booking.booking_id, exc)
    db.commit()

    if booking.google_event_id:
        from app.services.calendar_service import remove_booking_from_calendar

        remove_booking_from_calendar(booking, db)

    try:
        from app.services.conversation_service import post_system_message

        if refund_cents > 0:
            line = "The client cancelled this booking. It was within the 24-hour grace period, so they've been refunded in full."
        elif vendor_cents > 0:
            line = (
                f"The client cancelled this booking. ${vendor_cents / 100:,.2f} of what "
                "they paid has been sent to the vendor for holding the date."
            )
        elif booking.payment_method == "manual":
            line = "The client cancelled this booking. Any payment sent directly is between them and the vendor — Jorna didn't hold it."
        else:
            line = "The client cancelled this booking."
        post_system_message(
            booking_id=booking.booking_id, sender_user_id=caller_user_id, content=line,
            meta={"kind": "cancellation", "refund_cents": refund_cents, "vendor_cents": vendor_cents},
            db=db,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("cancel_booking: couldn't post system message for %s: %s", booking.booking_id, exc)

    logger.info(
        "Booking %s cancelled: %d refunded to client, %d transferred to vendor",
        booking.booking_id, refund_cents, vendor_cents,
    )

    if booking.payment_method == "manual":
        message = "Booking cancelled. Any payment you sent directly is between you and the vendor — Jorna doesn't hold or refund it."
    elif refund_cents > 0:
        message = "Booking cancelled. You've been refunded in full — funds will return within 5–10 business days."
    elif vendor_cents > 0:
        message = (
            f"Booking cancelled. This is past the 24-hour grace period, so "
            f"${vendor_cents / 100:,.2f} goes to the vendor for holding the date — "
            "nothing is refunded to you."
        )
    else:
        message = "Booking cancelled."

    return {
        "message": message,
        "refund_cents": refund_cents,
        "vendor_cancellation_cents": vendor_cents,
        "payment_status": booking.payment_status,
    }


def cancel_booking(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    """The client cancelling a booking, at any point up to the event.

    Replaces the old flat request_refund. Within GRACE_HOURS of the vendor's
    acceptance, this is a full refund exactly as before. Past it, the client
    gets nothing back — the payment splits between the platform and the
    vendor instead (cancellation_split), the vendor's share moving out by the
    same Transfer mechanism _release_funds uses for a completed event.

    A manual-track booking skips all of that — Jorna never held the money,
    so cancelling it is just a status change (see _finish_cancellation).
    """
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise StripeError(404, "Booking not found")
    if booking.user_id != caller_user_id:
        raise StripeError(403, "You are not the customer for this booking")

    event_date = _parse_event_date(booking.date_iso)
    if event_date is not None:
        from app.services.booking_service import venue_today

        if venue_today(booking) >= event_date:
            raise StripeError(
                400,
                "This event has already happened, or is happening now — "
                "cancellation is no longer available. Raise a problem instead "
                "if something has gone wrong.",
            )

    if booking.payment_method == "manual":
        return _finish_cancellation(
            booking=booking, caller_user_id=caller_user_id, db=db,
            refund_cents=0, vendor_cents=0,
        )

    if booking.payment_status != PaymentStatus.PAID.value:
        raise StripeError(
            400,
            f"Booking is not eligible for cancellation (payment status: "
            f"'{booking.payment_status}')",
        )
    if not booking.payment_intent_id:
        raise StripeError(500, "No payment intent found for this booking")

    now = datetime.now(timezone.utc)
    split = cancellation_split(booking, now)
    refund_cents = split["refund_to_client_cents"]
    vendor_cents = split["vendor_cents"]

    if refund_cents > 0:
        try:
            stripe.Refund.create(
                payment_intent=booking.payment_intent_id,
                reason="requested_by_customer",
            )
        except stripe.StripeError as e:
            raise StripeError(502, f"Stripe refund failed: {e.user_message or str(e)}")
        booking.payment_status = PaymentStatus.REFUNDED.value
    elif vendor_cents > 0:
        vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
        if not vendor or not vendor.stripe_account_id:
            raise StripeError(500, "Vendor Stripe account not found — cannot pay out their share")
        try:
            stripe.Transfer.create(
                amount=vendor_cents,
                currency=booking.currency,
                destination=vendor.stripe_account_id,
                transfer_group=booking.booking_id,
                metadata={"booking_id": booking.booking_id, "reason": "cancellation"},
                idempotency_key=f"cancel_{booking.booking_id}",
            )
        except stripe.StripeError as e:
            raise StripeError(502, f"Stripe transfer failed: {e.user_message or str(e)}")
        booking.payment_status = PaymentStatus.CANCELLED.value
    else:
        # amount_cents was 0 or unset — nothing to move either way, but the
        # booking still needs to come off the books as cancelled.
        booking.payment_status = PaymentStatus.CANCELLED.value

    return _finish_cancellation(
        booking=booking, caller_user_id=caller_user_id, db=db,
        refund_cents=refund_cents, vendor_cents=vendor_cents,
    )


def mark_booking_paid(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    """The client attesting they've paid a manual-track vendor directly.

    Self-reported and unverifiable — Jorna never touches this money. This is
    the entire "payment status" a manual booking gets until the vendor
    confirms receiving it (see confirm_payment_received)."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise StripeError(404, "Booking not found")
    if booking.user_id != caller_user_id:
        raise StripeError(403, "You are not the customer for this booking")
    if booking.payment_method != "manual":
        raise StripeError(400, "This booking is on the protected track — payment happens automatically")

    from app.models.schemas import BookingStatus

    if booking.status != BookingStatus.APPROVED.value:
        raise StripeError(400, "This booking hasn't been accepted yet")
    _require_signed_contract(booking)
    if booking.payment_schedule:
        # "Paid in full" on a scheduled contract: everything not yet marked.
        return _client_mark_scheduled(
            booking, [i["id"] for i in booking.payment_schedule], db,
            nothing="Every payment is already marked as sent",
            reply=lambda: {"message": "Marked as paid.", "payment_status": booking.payment_status},
        )
    if booking.payment_status != PaymentStatus.UNPAID.value:
        raise StripeError(400, f"Already marked (payment status: '{booking.payment_status}')")

    now = datetime.now(timezone.utc)
    booking.payment_status = PaymentStatus.MARKED_PAID.value
    booking.manual_payment_marked_at = now
    db.commit()
    db.refresh(booking)

    try:
        from app.services.conversation_service import post_system_message

        post_system_message(
            booking_id=booking_id, sender_user_id=caller_user_id,
            content="The client marked this booking as paid.",
            meta={"kind": "manual_payment_marked"}, db=db,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("mark_booking_paid: couldn't post system message for %s: %s", booking_id, exc)

    return {"message": "Marked as paid.", "payment_status": booking.payment_status}


def confirm_payment_received(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    """The vendor attesting they received a manual-track client's direct
    payment. Same self-reported, unverifiable contract as mark_booking_paid."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise StripeError(404, "Booking not found")

    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    if not vendor or vendor.user_id != caller_user_id:
        raise StripeError(403, "You are not the vendor for this booking")
    if booking.payment_method != "manual":
        raise StripeError(400, "This booking is on the protected track — payment happens automatically")
    if booking.payment_schedule:
        # A scheduled contract (contract_document): confirm every payment the
        # client has marked sent — what "Confirm payment" meant before.
        return _confirm_scheduled(
            booking, [i["id"] for i in booking.payment_schedule if i.get("marked_paid_at")], db,
            nothing="Nothing to confirm yet — no payment has been marked as sent",
            reply=lambda: {"message": "Confirmed.", "payment_status": booking.payment_status},
        )
    if booking.payment_status != PaymentStatus.MARKED_PAID.value:
        raise StripeError(400, f"Nothing to confirm yet (payment status: '{booking.payment_status}')")

    now = datetime.now(timezone.utc)
    booking.payment_status = PaymentStatus.CONFIRMED_PAID.value
    booking.manual_payment_confirmed_at = now
    db.commit()
    db.refresh(booking)

    try:
        from app.services.conversation_service import post_system_message

        post_system_message(
            booking_id=booking_id, sender_user_id=caller_user_id,
            content="The vendor confirmed they received payment.",
            meta={"kind": "manual_payment_confirmed"}, db=db,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("confirm_payment_received: couldn't post system message for %s: %s", booking_id, exc)

    return {"message": "Confirmed.", "payment_status": booking.payment_status}


def mark_deposit_paid(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    """The client attesting they've sent the deposit. The authenticated
    sibling of guest_booking_service's public version, for a real-account
    booking that has a deposit configured — mirrors mark_booking_paid
    exactly, one step earlier (deposit, not the full/remaining balance).
    """
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise StripeError(404, "Booking not found")
    if booking.user_id != caller_user_id:
        raise StripeError(403, "You are not the customer for this booking")
    if booking.payment_method != "manual":
        raise StripeError(400, "This booking is on the protected track — payment happens automatically")
    _require_signed_contract(booking)
    if booking.payment_schedule and len(booking.payment_schedule) >= 2:
        return _client_mark_scheduled(
            booking, [booking.payment_schedule[0]["id"]], db,
            nothing="Deposit already marked as paid",
            reply=lambda: {
                "message": "Deposit marked as paid.",
                "deposit_marked_paid_at": booking.deposit_marked_paid_at.isoformat(),
            },
        )
    if booking.deposit_percent is None:
        raise StripeError(400, "This booking has no deposit configured")
    if booking.deposit_marked_paid_at is not None:
        raise StripeError(400, "Deposit already marked as paid")

    booking.deposit_marked_paid_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(booking)

    try:
        from app.services.conversation_service import post_system_message

        post_system_message(
            booking_id=booking_id, sender_user_id=caller_user_id,
            content="The client marked the deposit as paid.",
            meta={"kind": "deposit_marked_paid"}, db=db,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("mark_deposit_paid: couldn't post system message for %s: %s", booking_id, exc)

    return {"message": "Deposit marked as paid.", "deposit_marked_paid_at": booking.deposit_marked_paid_at.isoformat()}


def _require_signed_contract(booking: Booking) -> None:
    """An accepted request is a proposal until the client signs it
    (DECISIONS #17) — nothing is owed before then."""
    if booking.contract_token and booking.signed_at is None:
        raise StripeError(400, "Sign the contract first — the link is in the email your vendor sent")


def _client_mark_scheduled(booking: Booking, ids: list[str], db: Session, *, nothing: str, reply) -> dict:
    from app.services import contract_document as doc

    changed = doc.mark_paid(booking, ids)
    if not changed:
        raise StripeError(400, nothing)
    for i in changed:
        doc.record(db, booking, "payment_marked", "client", {
            "installment_id": i["id"], "label": i["label"], "amount_cents": i["amount_cents"],
        })
    db.commit()
    db.refresh(booking)
    return reply()


def _confirm_scheduled(booking: Booking, ids: list[str], db: Session, *, nothing: str, reply) -> dict:
    from app.services import contract_document as doc

    changed = doc.confirm_received(booking, ids)
    if not changed:
        raise StripeError(400, nothing)
    for i in changed:
        doc.record(db, booking, "payment_confirmed", "vendor", {
            "installment_id": i["id"], "label": i["label"], "amount_cents": i["amount_cents"],
        })
    db.commit()
    db.refresh(booking)
    return reply()


def confirm_deposit_received(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    """The vendor attesting they received the deposit. Works for a guest
    booking too, unlike mark_deposit_paid above -- this only checks the
    vendor's identity, never booking.user_id, so a guest booking (which has
    no authenticated client to call mark_deposit_paid at all -- see
    guest_booking_service's public sibling instead) still lets its vendor
    confirm normally."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise StripeError(404, "Booking not found")

    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    if not vendor or vendor.user_id != caller_user_id:
        raise StripeError(403, "You are not the vendor for this booking")
    if booking.payment_schedule and len(booking.payment_schedule) >= 2:
        first = booking.payment_schedule[0]
        if not first.get("marked_paid_at"):
            raise StripeError(400, "Nothing to confirm yet — the deposit hasn't been marked as paid")
        return _confirm_scheduled(
            booking, [first["id"]], db, nothing="Deposit already confirmed",
            reply=lambda: {
                "message": "Deposit confirmed.",
                "deposit_confirmed_received_at": booking.deposit_confirmed_received_at.isoformat(),
            },
        )
    if booking.deposit_percent is None:
        raise StripeError(400, "This booking has no deposit configured")
    if booking.deposit_marked_paid_at is None:
        raise StripeError(400, "Nothing to confirm yet — the deposit hasn't been marked as paid")
    if booking.deposit_confirmed_received_at is not None:
        raise StripeError(400, "Deposit already confirmed")

    booking.deposit_confirmed_received_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(booking)

    if booking.user_id is not None:
        try:
            from app.services.conversation_service import post_system_message

            post_system_message(
                booking_id=booking_id, sender_user_id=caller_user_id,
                content="The vendor confirmed they received the deposit.",
                meta={"kind": "deposit_confirmed"}, db=db,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("confirm_deposit_received: couldn't post system message for %s: %s", booking_id, exc)

    return {"message": "Deposit confirmed.", "deposit_confirmed_received_at": booking.deposit_confirmed_received_at.isoformat()}


# ── Refund after a reschedule falls through ───────────────────────────

# What a vendor keeps when they can't meet a date the client proposed.
#
# Deliberately close to the platform fee already taken, so it reads as "you keep
# what was already spent" rather than a penalty. That distinction matters here
# because of who triggers it: the *vendor* is the one who declined. A fee that
# looked punitive would be hardest to defend in exactly the case that produces
# it.
#
# One constant, and every client-facing string is built from it — so the
# disclosure at checkout and the arithmetic that runs later cannot drift apart.
RESCHEDULE_CANCELLATION_PCT = 10


def reschedule_refund_cents(booking: Booking, service=None) -> int:
    """What this booking refunds when a reschedule falls through."""
    from app.services.booking_service import resolve_total_cents

    paid = booking.amount_cents or resolve_total_cents(booking, service) or 0
    return int(round(paid * (100 - RESCHEDULE_CANCELLATION_PCT) / 100))


def refund_after_failed_reschedule(
    *, booking_id: str, caller_user_id: str, db: Session
) -> dict:
    """Refund a booking whose reschedule the vendor declined, or let lapse.

    Not the ordinary 24-hour refund. That window is about changing your mind
    shortly after paying; this is about a vendor being unable to supply what is
    now being asked for, which can happen at any distance from the event. So it
    does not reopen the 24-hour window, and it doesn't check it — it has its own
    gate, which is that a change request on this booking was declined or expired
    and the client has not already acted on it.

    Partial by decision: the vendor held a date and turned down other work, and
    keeps RESCHEDULE_CANCELLATION_PCT of the total for having done so.
    """
    from app.db.models import ChangeRequest

    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise StripeError(404, "Booking not found")
    if booking.user_id != caller_user_id:
        raise StripeError(403, "You are not the customer for this booking")
    if booking.payment_status not in (PaymentStatus.PAID.value, PaymentStatus.PROCESSING.value):
        raise StripeError(
            400,
            f"Booking is not eligible for a refund (payment status: "
            f"'{booking.payment_status}')",
        )

    # The gate. Without a fallen-through request this is just an ordinary
    # cancellation, and cancel_booking already answers that.
    settled = (
        db.query(ChangeRequest)
        .filter(
            ChangeRequest.booking_id == booking_id,
            ChangeRequest.status.in_(("declined", "expired")),
        )
        .order_by(ChangeRequest.created_at.desc())
        .first()
    )
    if not settled:
        raise StripeError(
            400,
            "This booking has no declined or expired date change, so the "
            "ordinary refund rules apply.",
        )

    if not booking.payment_intent_id:
        raise StripeError(500, "No payment intent found for this booking")

    service = (
        db.query(Service).filter(Service.service_id == booking.service_id).first()
    )
    amount = reschedule_refund_cents(booking, service)
    try:
        stripe.Refund.create(
            payment_intent=booking.payment_intent_id,
            amount=amount,
            reason="requested_by_customer",
        )
    except stripe.StripeError as e:
        raise StripeError(502, f"Stripe refund failed: {e.user_message or str(e)}")

    booking.payment_status = PaymentStatus.REFUNDED.value
    # Dead as well as refunded: the vendor can't make the date, so this booking
    # isn't happening. Leaving it "approved" would keep it in the plan's live
    # section and in every count derived from one.
    from app.models.schemas import BookingStatus, RejectionReason

    booking.status = BookingStatus.REJECTED.value
    booking.rejected_reason = RejectionReason.RESCHEDULE_FAILED.value
    try:
        from app.services.booking_service import sync_event_venue
        sync_event_venue(booking.bundle_id, db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reschedule refund: venue re-sync failed for %s: %s", booking_id, exc)
    db.commit()

    if booking.google_event_id:
        from app.services.calendar_service import remove_booking_from_calendar

        remove_booking_from_calendar(booking, db)

    logger.info(
        "Reschedule refund of %s cents issued for booking %s", amount, booking_id
    )

    kept = (booking.amount_cents or 0) - amount
    return {
        "message": (
            f"Refunded ${amount / 100:,.2f}. "
            f"${kept / 100:,.2f} ({RESCHEDULE_CANCELLATION_PCT}%) is retained as "
            "the cancellation fee for the date your vendor held."
        ),
        "refunded_cents": amount,
        "retained_cents": kept,
    }


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
    if booking.payment_status != PaymentStatus.PAID.value:
        raise StripeError(
            400,
            f"Disputes can only be raised while payment is held on the platform "
            f"(current status: '{booking.payment_status}'). "
            "If funds were already released, contact support directly."
        )

    booking.payment_status = PaymentStatus.DISPUTED.value
    db.commit()
    logger.info("Dispute raised for booking %s by user %s", booking_id, caller_user_id)
    return {
        "message": "Dispute raised. Our team will review and resolve it within 3–5 business days.",
        "booking_id": booking_id,
        "payment_status": PaymentStatus.DISPUTED.value,
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
    if booking.payment_status != PaymentStatus.DISPUTED.value:
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
        booking.payment_status = PaymentStatus.REFUNDED.value
        try:
            from app.services.booking_service import sync_event_venue
            sync_event_venue(booking.bundle_id, db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve_dispute: venue re-sync failed for %s: %s", booking_id, exc)
        db.commit()

        if booking.google_event_id:
            from app.services.calendar_service import remove_booking_from_calendar

            remove_booking_from_calendar(booking, db)

        logger.info("Dispute resolved: refund issued for booking %s", booking_id)
        return {
            "message": "Dispute resolved. Customer has been refunded.",
            "payment_status": PaymentStatus.REFUNDED.value,
        }

    # release_vendor — transfer funds to vendor
    _release_funds(booking, db)
    logger.info("Dispute resolved: funds released to vendor for booking %s", booking_id)
    return {
        "message": "Dispute resolved. Funds have been released to the vendor.",
        "payment_status": PaymentStatus.RELEASED.value,
    }


# ── Card on file ──────────────────────────────────────────────────────
#
# Payment used to be the client's job after the fact: send the plan, wait for
# each vendor, then come back and press Pay on every one that accepted. Plenty
# never came back, and a vendor who had held a date was left waiting on
# something that was nobody's next action.
#
# The card is collected once, when the plan is sent, and charged the moment a
# vendor accepts. Money moves at exactly the same point in the story as before —
# this changes where the card is captured, not when the charge happens, so
# escrow, release, refunds and disputes are all untouched.
#
# Deliberately not a charge up front. A plan has four to eight vendors and any
# of them may decline; charging on send would mean refunding each decline, and
# Stripe does not return the processing fee on a refund. Saving a card costs
# nothing when a vendor says no.


def _stripe_customer(user: User, db: Session) -> str:
    """The Stripe Customer for this user, created on first use."""
    if user.stripe_customer_id:
        return user.stripe_customer_id
    try:
        customer = stripe.Customer.create(
            email=user.email,
            name=f"{user.f_name} {user.l_name}".strip() or None,
            metadata={"user_id": user.user_id},
        )
    except stripe.StripeError as e:
        raise StripeError(502, f"Stripe error: {e.user_message or str(e)}")
    user.stripe_customer_id = customer.id
    db.commit()
    return customer.id


def create_card_setup_session(*, user_id: str, db: Session, return_base: str) -> dict:
    """A Stripe-hosted page for saving a card, with no charge attached.

    Checkout in setup mode rather than an inline element: the same hosted form
    the rest of this integration uses, so card details never reach our servers
    and 3-D Secure stays Stripe's problem rather than ours.
    """
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise StripeError(404, "User not found")

    customer_id = _stripe_customer(user, db)
    base = return_base.rstrip("/")
    try:
        session = stripe.checkout.Session.create(
            mode="setup",
            customer=customer_id,
            payment_method_types=["card"],
            metadata={"user_id": user_id},
            success_url=f"{base}/card-saved/?status=success",
            cancel_url=f"{base}/card-saved/?status=cancel",
        )
    except stripe.StripeError as e:
        raise StripeError(502, f"Stripe error: {e.user_message or str(e)}")

    return {"setup_url": session.url}


def saved_card(*, user_id: str, db: Session) -> dict:
    """What is on file, for a screen that needs to say."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise StripeError(404, "User not found")
    return {
        "has_card": bool(user.stripe_payment_method_id),
        "brand": user.card_brand,
        "last4": user.card_last4,
    }


def sync_saved_card(*, user_id: str, db: Session) -> dict:
    """Adopt the newest card on the customer as the one we will charge.

    Called when the client returns from the hosted form. Reads Stripe rather
    than trusting the redirect, and is idempotent — landing on that page twice
    settles on the same card.
    """
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise StripeError(404, "User not found")
    if not user.stripe_customer_id:
        return saved_card(user_id=user_id, db=db)

    try:
        methods = stripe.PaymentMethod.list(customer=user.stripe_customer_id, type="card")
    except stripe.StripeError as e:
        raise StripeError(502, f"Stripe error: {e.user_message or str(e)}")

    newest = max(methods.data, key=lambda m: m.created, default=None)
    if newest is not None:
        card = _sv(newest, "card") or {}
        user.stripe_payment_method_id = newest.id
        user.card_brand = _sv(card, "brand")
        user.card_last4 = _sv(card, "last4")
        db.commit()
        logger.info("Saved card %s for user %s", newest.id, user_id)

    return saved_card(user_id=user_id, db=db)


def forget_saved_card(*, user_id: str, db: Session) -> dict:
    """Detach the card. The Customer stays — it is their billing identity."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise StripeError(404, "User not found")
    if user.stripe_payment_method_id:
        try:
            stripe.PaymentMethod.detach(user.stripe_payment_method_id)
        except stripe.StripeError as exc:
            # Already detached, or never really there. Forgetting it here is the
            # part that matters.
            logger.warning("Detaching card for %s failed: %s", user_id, exc)
    user.stripe_payment_method_id = None
    user.card_brand = None
    user.card_last4 = None
    db.commit()
    return {"has_card": False}


class CardChargeUnavailable(Exception):
    """No card on file, or nothing chargeable about this booking yet.

    Distinct from a card that was tried and declined: this one means do not try.
    """


def charge_saved_card(*, booking_id: str, db: Session) -> dict:
    """Charge the client's saved card for a booking a vendor has just accepted.

    Off-session: the client is not at the keyboard, and authorised this when
    they sent the plan. A card that needs 3-D Secure now will decline, which is
    why the caller must leave the booking payable by hand rather than treat a
    failure as fatal.

    Everything downstream is unchanged — same amount, same platform fee, same
    transfer_group — so escrow, release, refund and dispute behave exactly as
    they do for a booking paid through Checkout.
    """
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise CardChargeUnavailable("Booking not found")
    if booking.payment_status not in (PaymentStatus.UNPAID.value, PaymentStatus.PROCESSING.value):
        raise CardChargeUnavailable(f"Already {booking.payment_status}")

    user = db.query(User).filter(User.user_id == booking.user_id).first()
    if not user or not user.stripe_payment_method_id or not user.stripe_customer_id:
        raise CardChargeUnavailable("No card on file")

    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    if not service:
        raise CardChargeUnavailable("Service not found")

    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    if not vendor or not vendor.stripe_onboarding_complete:
        raise CardChargeUnavailable("Vendor cannot receive payouts yet")

    from app.services.booking_service import resolve_total_cents

    amount_cents = resolve_total_cents(booking, service)
    if amount_cents is None:
        # Rate-priced with an unknown quantity. Refuse rather than charge the
        # bare per-unit rate as though it were the total.
        raise CardChargeUnavailable("Total not resolvable yet")

    platform_fee_cents = round(amount_cents * PLATFORM_FEE_PERCENT / 100)

    intent = stripe.PaymentIntent.create(
        amount=amount_cents,
        currency=booking.currency,
        customer=user.stripe_customer_id,
        payment_method=user.stripe_payment_method_id,
        off_session=True,
        confirm=True,
        transfer_group=booking_id,
        metadata={
            "booking_id": booking_id,
            "vendor_id": booking.vendor_id,
            "user_id": booking.user_id,
            "charged": "on_vendor_acceptance",
        },
        # One charge per booking, however many times acceptance is retried.
        idempotency_key=f"accept_{booking_id}",
    )

    booking.payment_intent_id = intent.id
    booking.amount_cents = amount_cents
    booking.platform_fee_cents = platform_fee_cents
    # The webhook is what normally marks a booking paid, and it still will. This
    # mirrors it for the common case where the intent succeeds inline; both
    # paths write the same fields, and the webhook is idempotent.
    if intent.status == "succeeded":
        booking.payment_status = PaymentStatus.PAID.value
        booking.paid_at = datetime.now(timezone.utc)
    else:
        booking.payment_status = PaymentStatus.PROCESSING.value
    db.commit()

    logger.info(
        "Charged saved card for booking %s: %s (%d cents)",
        booking_id, intent.status, amount_cents,
    )
    return {"payment_status": booking.payment_status, "amount_cents": amount_cents}
