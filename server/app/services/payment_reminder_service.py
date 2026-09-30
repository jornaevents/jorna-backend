"""Reminders for a contract's scheduled payments (docs/DECISIONS.md #18).

A signed contract says when each payment is due, and until this nothing said
it again: the balance "due 14 days before the event" was paid if the client
remembered, or after the vendor chased it by text. Jorna never touches the
money, so all this does is tell people.

- The client, 3 days before a payment is due and on the day, with the link to
  their contract page where they can mark it sent.
- The vendor, once a payment is 3 days overdue and still not marked sent, so
  the chasing starts from a fact rather than a feeling.

Each reminder is recorded as a `payment_reminder` event on the contract's
timeline, which is also what stops it being sent twice — the sweep can run
as often as it likes. When a sweep finds a payment past more than one of
those moments (a restart, or a contract signed late), it sends only the most
recent one rather than a burst.

Dates are UTC calendar days, like `due_on` itself.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import WEB_APP_URL
from app.db.models import Booking, ContractEvent, Service, User, Vendor
from app.models.schemas import BookingStatus
from app.services import contract_document as doc
from app.services.email_service import send_email
from app.utils.notifications import notify_vendor_contract_event

logger = logging.getLogger(__name__)

UPCOMING_LEAD = timedelta(days=3)
OVERDUE_AFTER = timedelta(days=3)

# Voided, declined and cancelled contracts owe nothing.
LIVE_STATUSES = (BookingStatus.APPROVED.value, BookingStatus.PAYMENT_CONFIRMED.value)


def _pretty(d: date) -> str:
    return f"{d.strftime('%A, %B')} {d.day}"


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def reminder_due(installment: dict, booking: Booking, today: date) -> str | None:
    """Which reminder this payment is owed today, if any: "overdue" (to the
    vendor), "due" or "upcoming" (to the client) — the latest that applies.
    None once the client has marked it sent or the vendor confirmed it."""
    if installment.get("marked_paid_at") or installment.get("confirmed_at"):
        return None
    due = doc.effective_due(installment, booking)
    if due is None:
        return None
    if today >= due + OVERDUE_AFTER:
        return "overdue"
    if today >= due:
        return "due"
    # "Due when signed" is due the day they signed — the signed receipt
    # already said so, so there's nothing to warn about ahead of time.
    if installment.get("due_type") != "on_signing" and today >= due - UPCOMING_LEAD:
        return "upcoming"
    return None


def _already_sent(booking: Booking, db: Session) -> set[tuple[str, str]]:
    rows = (
        db.query(ContractEvent)
        .filter(ContractEvent.booking_id == booking.booking_id, ContractEvent.kind == "payment_reminder")
        .all()
    )
    return {((r.detail or {}).get("installment_id"), (r.detail or {}).get("reminder")) for r in rows}


def _client_email(booking: Booking, installment: dict, due: date, kind: str, vendor_name: str) -> None:
    link = f"{WEB_APP_URL}/booking-link?t={booking.contract_token}"
    amount = _money(installment["amount_cents"])
    when = "today" if kind == "due" else f"on {_pretty(due)}"
    send_email(
        to=booking.guest_email,
        subject=f"Your {installment['label'].lower()} for {vendor_name} is due {when}",
        html=(
            f"<p>{installment['label']}: <strong>{amount}</strong>, due {when}, for your booking with "
            f"{vendor_name} on {_pretty(date.fromisoformat(booking.date_iso))}.</p>"
            "<p>You pay them directly — Jorna doesn't handle the money. Once you've sent it, "
            f'<a href="{link}">mark it as sent</a> so they know to look for it.</p>'
        ),
    )


def _remind(booking: Booking, installment: dict, kind: str, due: date, db: Session) -> None:
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    vendor_user = db.query(User).filter(User.user_id == vendor.user_id).first() if vendor else None
    vendor_name = f"{vendor_user.f_name} {vendor_user.l_name}".strip() if vendor_user else "your vendor"

    if kind == "overdue":
        service = db.query(Service).filter(Service.service_id == booking.service_id).first()
        client = booking.guest_name or "Your client"
        notify_vendor_contract_event(
            vendor_user=vendor_user,
            title=f"{client}'s {installment['label'].lower()} is overdue",
            body=(
                f"{_money(installment['amount_cents'])} was due {_pretty(due)} for "
                f"{service.name if service else 'your booking'} on {_pretty(date.fromisoformat(booking.date_iso))}, "
                "and they haven't marked it sent. If it arrived, confirm it on the contract's page."
            ),
            booking_id=booking.booking_id, event="contract_payment_overdue", db=db,
        )
    else:
        if not booking.guest_email:
            return
        _client_email(booking, installment, due, kind, vendor_name)

    doc.record(db, booking, "payment_reminder", "system", {
        "installment_id": installment["id"],
        "label": installment["label"],
        "amount_cents": installment["amount_cents"],
        "reminder": kind,
        "due_on": due.isoformat(),
    })


def send_payment_reminders(*, db: Session, now: datetime | None = None) -> int:
    """One sweep. Returns how many reminders went out."""
    today = (now or datetime.now(timezone.utc)).date()
    bookings = (
        db.query(Booking)
        .filter(
            Booking.payment_schedule.isnot(None),
            Booking.signed_at.isnot(None),
            Booking.status.in_(LIVE_STATUSES),
        )
        .all()
    )
    sent = 0
    for booking in bookings:
        try:
            done = _already_sent(booking, db)
            for installment in booking.payment_schedule or []:
                kind = reminder_due(installment, booking, today)
                if kind is None or (installment["id"], kind) in done:
                    continue
                _remind(booking, installment, kind, doc.effective_due(installment, booking), db)
                db.commit()
                sent += 1
        except Exception as exc:  # one bad contract must not stop the rest
            logger.warning("Payment reminders for booking %s failed: %s", booking.booking_id, exc)
            db.rollback()
    if sent:
        logger.info("Sent %d payment reminders", sent)
    return sent
