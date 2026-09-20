"""The client-facing, no-login side of a guest/contract booking — see
docs/DECISIONS.md #13. Every function here is reached by a public,
unauthenticated endpoint; the `contract_token` is the entire security
boundary (same trust model as the existing RSVP system's `Guest.token`).
Nothing here accepts a `caller_user_id` — there isn't one.

The vendor-authenticated side (create/edit a contract, confirm receiving a
payment) lives in contract_service.py / stripe_service.py instead.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Booking, Service, User, Vendor
from app.models.schemas import PaymentStatus
from app.services.email_service import send_email


class GuestBookingError(Exception):
    """Raised when a guest-booking operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _by_token(contract_token: str, db: Session) -> Booking:
    booking = db.query(Booking).filter(Booking.contract_token == contract_token).first()
    if not booking or booking.user_id is not None:
        # A non-guest booking has no contract_token in practice, but the
        # user_id check is the real boundary — never serve a real account's
        # booking off of anything token-shaped.
        raise GuestBookingError(404, "Booking not found")
    return booking


def _guest_dict(booking: Booking, service: Service | None, vendor: Vendor | None, vendor_user: User | None) -> dict:
    return {
        "booking_id": booking.booking_id,
        "vendor_display_name": f"{vendor_user.f_name} {vendor_user.l_name}".strip() if vendor_user else None,
        "vendor_venmo_handle": vendor.venmo_handle if vendor else None,
        "vendor_zelle_contact": vendor.zelle_contact if vendor else None,
        "service_name": service.name if service else None,
        "date_iso": booking.date_iso,
        "date_end": booking.date_end,
        "time_start": booking.time_start,
        "time_end": booking.time_end,
        "location": booking.location,
        "guest_count": booking.guest_count,
        "amount_cents": booking.amount_cents,
        "deposit_percent": booking.deposit_percent,
        "deposit_amount_cents": booking.deposit_amount_cents,
        "cancellation_window_hours": booking.cancellation_window_hours,
        "overtime_rate_cents": booking.overtime_rate_cents,
        "addon_rate_cents": booking.addon_rate_cents,
        "contract_terms": booking.contract_terms,
        "guest_name": booking.guest_name,
        "guest_email": booking.guest_email,
        "guest_phone": booking.guest_phone,
        "signer_name": booking.signer_name,
        "signed_at": booking.signed_at.isoformat() if booking.signed_at else None,
        "payment_status": booking.payment_status,
        "deposit_marked_paid_at": booking.deposit_marked_paid_at.isoformat() if booking.deposit_marked_paid_at else None,
        "deposit_confirmed_received_at": booking.deposit_confirmed_received_at.isoformat() if booking.deposit_confirmed_received_at else None,
    }


def get_guest_booking(*, contract_token: str, db: Session) -> dict:
    booking = _by_token(contract_token, db)
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    vendor_user = db.query(User).filter(User.user_id == vendor.user_id).first() if vendor else None
    return _guest_dict(booking, service, vendor, vendor_user)


def fill_details(
    *,
    contract_token: str,
    guest_name: str | None,
    guest_email: str | None,
    guest_phone: str | None,
    location: str | None,
    guest_count: int | None,
    db: Session,
) -> dict:
    """The client's own contact + venue info. Refused once signed — the
    agreement is what was signed, not whatever gets typed in afterward."""
    booking = _by_token(contract_token, db)
    if booking.signed_at is not None:
        raise GuestBookingError(400, "This booking has already been signed and can no longer be edited")

    if guest_name is not None:
        booking.guest_name = guest_name.strip() or None
    if guest_email is not None:
        booking.guest_email = guest_email.strip() or None
    if guest_phone is not None:
        booking.guest_phone = guest_phone.strip() or None
    if location is not None:
        booking.location = location.strip() or "TBD"
    if guest_count is not None:
        booking.guest_count = guest_count

    db.commit()
    db.refresh(booking)
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    vendor_user = db.query(User).filter(User.user_id == vendor.user_id).first() if vendor else None
    return _guest_dict(booking, service, vendor, vendor_user)


def sign_contract(*, contract_token: str, signer_name: str, db: Session) -> dict:
    """The client's e-signature — a typed full legal name, nothing more.
    Not a verified identity: the token is the only credential this whole
    flow has (see docs/DECISIONS.md #13's accepted-risk note). Requires an
    email on file since signing is the moment we send the client their only
    durable copy of the agreement -- there's no account to log back into."""
    booking = _by_token(contract_token, db)
    if booking.signed_at is not None:
        raise GuestBookingError(400, "This booking has already been signed")
    if not signer_name or not signer_name.strip():
        raise GuestBookingError(400, "Type your full legal name to sign")
    if not booking.guest_email:
        raise GuestBookingError(400, "Add your email first so we can send you a copy of this agreement")

    booking.signer_name = signer_name.strip()
    booking.signed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(booking)

    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    vendor_user = db.query(User).filter(User.user_id == vendor.user_id).first() if vendor else None
    vendor_name = f"{vendor_user.f_name} {vendor_user.l_name}".strip() if vendor_user else "your vendor"

    _send_signed_receipt(booking, service, vendor, vendor_name)

    return _guest_dict(booking, service, vendor, vendor_user)


def _send_signed_receipt(booking: Booking, service: Service | None, vendor: Vendor | None, vendor_name: str) -> None:
    """Best-effort — a failed send must not fail the signature itself. The
    email is the client's only record of this agreement, but the signature
    is already committed by the time this runs."""
    payment_line = (
        f"A {booking.deposit_percent}% deposit (${(booking.deposit_amount_cents or 0) / 100:,.2f}) is due"
        if booking.deposit_percent is not None
        else f"${(booking.amount_cents or 0) / 100:,.2f} is due"
    )
    pay_to = []
    if vendor and vendor.venmo_handle:
        pay_to.append(f"Venmo: {vendor.venmo_handle}")
    if vendor and vendor.zelle_contact:
        pay_to.append(f"Zelle: {vendor.zelle_contact}")
    pay_to_line = " · ".join(pay_to) if pay_to else "the contact info your vendor gave you"

    html = f"""
    <p>Your booking with {vendor_name} is confirmed.</p>
    <p><strong>{service.name if service else 'Service'}</strong><br>
    {booking.date_iso}, {booking.time_start}–{booking.time_end}<br>
    {booking.location}</p>
    <p>Total: ${(booking.amount_cents or 0) / 100:,.2f}<br>
    {payment_line}</p>
    <p>You'll pay {vendor_name} directly — Jorna doesn't handle the money.
    {pay_to_line}</p>
    <p>Signed by {booking.signer_name} on {booking.signed_at.strftime('%B %d, %Y')}.</p>
    """
    send_email(
        to=booking.guest_email,
        subject=f"Your booking with {vendor_name} is confirmed",
        html=html,
    )


def mark_full_paid(*, contract_token: str, db: Session) -> dict:
    """The guest sibling of stripe_service.mark_booking_paid — no session,
    so the token is the only proof this is the right person."""
    booking = _by_token(contract_token, db)
    if booking.signed_at is None:
        raise GuestBookingError(400, "This booking hasn't been signed yet")
    if booking.payment_status != PaymentStatus.UNPAID.value:
        raise GuestBookingError(400, f"Already marked (payment status: '{booking.payment_status}')")

    booking.payment_status = PaymentStatus.MARKED_PAID.value
    booking.manual_payment_marked_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(booking)
    return {"message": "Marked as paid.", "payment_status": booking.payment_status}


def mark_deposit_paid(*, contract_token: str, db: Session) -> dict:
    """The guest sibling of stripe_service.mark_deposit_paid."""
    booking = _by_token(contract_token, db)
    if booking.signed_at is None:
        raise GuestBookingError(400, "This booking hasn't been signed yet")
    if booking.deposit_percent is None:
        raise GuestBookingError(400, "This booking has no deposit configured")
    if booking.deposit_marked_paid_at is not None:
        raise GuestBookingError(400, "Deposit already marked as paid")

    booking.deposit_marked_paid_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(booking)
    return {
        "message": "Deposit marked as paid.",
        "deposit_marked_paid_at": booking.deposit_marked_paid_at.isoformat(),
    }
