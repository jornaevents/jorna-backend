"""The vendor's earnings ledger (docs/DECISIONS.md #33): one row per payment
on every signed contract, past and present, each tied to the contract that
set it — and the two things a vendor does from it besides confirming money
arrived: remind the client, and take back a mark that was wrong.

What a row is:
- a payment on a signed, unvoided contract's schedule;
- or, for a booking made before contracts (no contract_token, accepted by the
  vendor), one "Full amount" row from the booking's own payment fields.

Status is decided here, once, so every page agrees: Received > Client says
sent > Cancelled > Overdue > Due soon (7 days) > Scheduled. Days are UTC
calendar days, like the payment reminders.

Nothing here needs its own table. A payment's marks live in the booking's
payment_schedule JSON (plus `paid_via`); who did what, and when, is the
contract's event log.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from html import escape

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import WEB_APP_URL
from app.db.models import (
    Booking,
    Bundle,
    ContractDocument,
    ContractEvent,
    Service,
    User,
    Vendor,
)
from app.services import contract_document as doc
from app.services import payment_reminder_service as reminders
from app.services.email_service import send_email
from app.utils.timeutil import utc_iso

logger = logging.getLogger(__name__)

DUE_SOON = timedelta(days=7)
PAYMENT_EVENTS = ("payment_marked", "payment_confirmed", "payment_reminder", "payment_not_received", "payment_unmarked")
RECEIVED_STATUSES = ("confirmed_paid", "released", "paid")


class LedgerError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _vendor_for(caller_user_id: str, db: Session) -> Vendor:
    vendor = db.query(Vendor).filter(Vendor.user_id == caller_user_id).first()
    if not vendor:
        raise LedgerError(403, "You must be a vendor to do this")
    return vendor


def _date(raw: str | None) -> date | None:
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _by_date(due: date | None, today: date) -> str:
    if due is None:
        return "scheduled"
    if due < today:
        return "overdue"
    if due <= today + DUE_SOON:
        return "due_soon"
    return "scheduled"


def row_status(installment: dict, booking: Booking, *, cancelled: bool, today: date) -> str:
    """One scheduled payment's status — the rules in the module docstring."""
    if installment.get("confirmed_at"):
        return "received"
    if installment.get("marked_paid_at"):
        return "client_says_sent"
    if cancelled:
        return "cancelled"
    return _by_date(doc.effective_due(installment, booking), today)


def _legacy_status(booking: Booking, *, cancelled: bool, today: date) -> str:
    status = booking.payment_status
    if status in RECEIVED_STATUSES:
        return "received"
    if status == "marked_paid":
        return "client_says_sent"
    if cancelled or status in ("refunded", "cancelled"):
        return "cancelled"
    return _by_date(_date(booking.date_iso), today)


def _client_marked(events: list[ContractEvent]) -> bool:
    """Whether the client's own "I've sent this" stands, walking the
    payment's events in order: a confirmation fills in marked_paid_at too,
    so the field alone can't say."""
    marked = False
    for e in events:
        detail = e.detail or {}
        if e.kind == "payment_marked":
            marked = True
        elif e.kind == "payment_not_received" and (
            detail.get("step") == "unmarked" or not detail.get("kept_mark", True)
        ):
            marked = False
        elif e.kind == "payment_unmarked":
            # The client took back their own "I've sent this".
            marked = False
    return marked


def _event_dict(e: ContractEvent) -> dict:
    detail = e.detail or {}
    out = {"at": utc_iso(e.at), "kind": e.kind, "actor": e.actor}
    for key in ("paid_via", "reminder", "step"):
        if detail.get(key):
            out[key] = detail[key]
    return out


class _Context:
    """Everything the rows of a set of bookings need, fetched in a few
    queries rather than one per booking."""

    def __init__(self, bookings: list[Booking], db: Session):
        ids = [b.booking_id for b in bookings]
        service_ids = {b.service_id for b in bookings}
        bundle_ids = {b.bundle_id for b in bookings if b.bundle_id}
        user_ids = {b.user_id for b in bookings if b.user_id}
        self.services = {s.service_id: s for s in db.query(Service).filter(Service.service_id.in_(service_ids))} if service_ids else {}
        self.bundles = {x.bundle_id: x for x in db.query(Bundle).filter(Bundle.bundle_id.in_(bundle_ids))} if bundle_ids else {}
        self.users = {u.user_id: u for u in db.query(User).filter(User.user_id.in_(user_ids))} if user_ids else {}
        self.documents: dict[str, list[ContractDocument]] = {}
        self.events: dict[tuple[str, str], list[ContractEvent]] = {}
        if ids:
            for d in db.query(ContractDocument).filter(
                ContractDocument.booking_id.in_(ids), ContractDocument.status == "signed",
            ):
                self.documents.setdefault(d.booking_id, []).append(d)
            for e in (
                db.query(ContractEvent)
                .filter(ContractEvent.booking_id.in_(ids), ContractEvent.kind.in_(PAYMENT_EVENTS))
                .order_by(ContractEvent.at)
            ):
                iid = (e.detail or {}).get("installment_id")
                if iid:
                    self.events.setdefault((e.booking_id, iid), []).append(e)

    def cancellation(self, booking: Booking) -> ContractDocument | None:
        return next((d for d in self.documents.get(booking.booking_id, []) if d.kind == "cancellation"), None)

    def cancelled(self, booking: Booking) -> bool:
        return booking.cancelled_at is not None or booking.status == "rejected" or self.cancellation(booking) is not None

    def booking_dict(self, booking: Booking, today: date) -> dict:
        service = self.services.get(booking.service_id)
        bundle = self.bundles.get(booking.bundle_id) if booking.bundle_id else None
        user = self.users.get(booking.user_id) if booking.user_id else None
        client = booking.guest_name or (f"{user.f_name or ''} {user.l_name or ''}".strip() if user else "") or "Client"
        cancelled = self.cancelled(booking)
        last_day = _date(booking.date_end) or _date(booking.date_iso)
        state = "cancelled" if cancelled else "done" if last_day and last_day < today else "upcoming"
        has_contract = booking.contract_token is not None and booking.signed_at is not None
        cancellation = self.cancellation(booking)
        return {
            "booking_id": booking.booking_id,
            "client_name": client,
            "event_name": (bundle.event_name if bundle else None) or (service.name if service else None),
            "event_date": booking.date_iso,
            "event_date_end": booking.date_end,
            "location": booking.location,
            "state": state,
            "total_cents": booking.amount_cents,
            "has_contract": has_contract,
            "contract_title": (booking.document_title or (f"{service.name} agreement" if service else "Agreement"))
            if has_contract else None,
            "signed_at": utc_iso(booking.signed_at),
            "signed_by": booking.signer_name,
            "addenda_count": sum(1 for d in self.documents.get(booking.booking_id, []) if d.kind == "addendum"),
            "cancellation_document_id": cancellation.document_id if cancellation else None,
        }

    def rows(self, booking: Booking, today: date) -> list[dict]:
        info = self.booking_dict(booking, today)
        cancelled = info["state"] == "cancelled"
        schedule = booking.payment_schedule if info["has_contract"] else None
        if not schedule:
            status = _legacy_status(booking, cancelled=cancelled, today=today)
            return [{
                "row_id": f"{booking.booking_id}:full",
                "booking_id": booking.booking_id,
                "installment_id": None,
                "label": "Full amount",
                "sequence": 1,
                "count": 1,
                "amount_cents": booking.amount_cents or 0,
                "due_on": booking.date_iso,
                "due_type": "date",
                "due_days": None,
                "status": status,
                "marked_paid_at": utc_iso(booking.manual_payment_marked_at),
                "marked_by_client": booking.manual_payment_marked_at is not None,
                "paid_via": None,
                "confirmed_at": utc_iso(booking.manual_payment_confirmed_at or booking.paid_at),
                "reminded_at": None,
                "can_remind": False,
                "can_undo": False,
                "events": [],
                "booking": info,
            }]
        out = []
        for n, item in enumerate(schedule, start=1):
            events = self.events.get((booking.booking_id, item["id"]), [])
            status = row_status(item, booking, cancelled=cancelled, today=today)
            manual = [e for e in events if e.kind == "payment_reminder" and (e.detail or {}).get("reminder") == "manual"]
            reminded_at = manual[-1].at if manual else None
            due = doc.effective_due(item, booking)
            out.append({
                "row_id": f"{booking.booking_id}:{item['id']}",
                "booking_id": booking.booking_id,
                "installment_id": item["id"],
                "label": item["label"],
                "sequence": n,
                "count": len(schedule),
                "amount_cents": item["amount_cents"],
                "due_on": due.isoformat() if due else None,
                "due_type": item.get("due_type"),
                "due_days": item.get("due_days"),
                "status": status,
                "marked_paid_at": utc_iso(item.get("marked_paid_at")),
                "marked_by_client": _client_marked(events),
                "paid_via": item.get("paid_via"),
                "confirmed_at": utc_iso(item.get("confirmed_at")),
                "reminded_at": utc_iso(reminded_at),
                "can_remind": status == "overdue" and bool(booking.guest_email)
                and not (reminded_at and reminded_at.date() == today),
                "can_undo": status in ("received", "client_says_sent"),
                "events": [_event_dict(e) for e in events],
                "booking": info,
            })
        return out


def _ledger_bookings(vendor_id: str, db: Session):
    return db.query(Booking).filter(
        Booking.vendor_id == vendor_id,
        or_(
            # A signed, unvoided contract.
            (Booking.contract_token.isnot(None)) & (Booking.signed_at.isnot(None)) & (Booking.voided_at.is_(None)),
            # Booked before contracts: the vendor accepted the request.
            (Booking.contract_token.is_(None)) & (Booking.confirmed_at.isnot(None)),
        ),
    )


def _totals(rows: list[dict]) -> dict:
    def total(statuses):
        picked = [r for r in rows if r["status"] in statuses]
        return sum(r["amount_cents"] for r in picked), len(picked)

    received, received_n = total(("received",))
    to_come, to_come_n = total(("scheduled", "due_soon"))
    overdue, overdue_n = total(("overdue",))
    waiting, waiting_n = total(("client_says_sent",))
    return {
        "received_cents": received, "received_count": received_n,
        "to_come_cents": to_come, "to_come_count": to_come_n,
        "overdue_cents": overdue, "overdue_count": overdue_n,
        "waiting_cents": waiting, "waiting_count": waiting_n,
    }


def vendor_ledger(*, caller_user_id: str, year: int | None, db: Session) -> dict:
    """Every payment the vendor's signed bookings carry. `year` keeps the
    payments due that year; None is all time. A booking's other payments
    come along in `schedules`, so a by-booking view can show its whole
    schedule whatever the filter."""
    vendor = _vendor_for(caller_user_id, db)
    today = _today()
    bookings = _ledger_bookings(vendor.vendor_id, db).all()
    ctx = _Context(bookings, db)
    every = [row for b in bookings for row in ctx.rows(b, today)]
    rows = [r for r in every if year is None or (r["due_on"] or "")[:4] == str(year)]
    rows.sort(key=lambda r: (r["due_on"] or "", r["booking"]["client_name"], r["sequence"]))
    shown = {r["booking_id"] for r in rows}
    in_year = {r["row_id"] for r in rows}
    return {
        "year": year,
        "today": today.isoformat(),
        "rows": rows,
        "totals": _totals(rows),
        # The rest of each shown booking's schedule, outside the year.
        "other_rows": [r for r in every if r["booking_id"] in shown and r["row_id"] not in in_year],
        "payment_methods": {"venmo": vendor.venmo_handle, "zelle": vendor.zelle_contact},
    }


def _row(booking: Booking, installment_id: str, db: Session) -> dict:
    ctx = _Context([booking], db)
    return next(r for r in ctx.rows(booking, _today()) if r["installment_id"] == installment_id)


def _own_booking_payment(booking_id: str, installment_id: str, caller_user_id: str, db: Session):
    vendor = _vendor_for(caller_user_id, db)
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking or booking.vendor_id != vendor.vendor_id or booking.signed_at is None or booking.voided_at:
        raise LedgerError(404, "We couldn't find that booking")
    try:
        item = doc.installment(booking, installment_id)
    except doc.DocumentError:
        raise LedgerError(404, "That payment isn't on this contract")
    return vendor, booking, item


def remind(*, booking_id: str, installment_id: str, caller_user_id: str, db: Session) -> dict:
    """Email the client about one overdue payment — once a day."""
    _, booking, item = _own_booking_payment(booking_id, installment_id, caller_user_id, db)
    row = _row(booking, installment_id, db)
    if row["status"] != "overdue":
        raise LedgerError(400, "Only an overdue payment can be reminded")
    if not booking.guest_email:
        raise LedgerError(400, "There's no email for this client — contact them yourself")
    if reminders.reminded_today(booking, installment_id, db, today=_today()):
        raise LedgerError(429, "You've already reminded them about this today")
    reminders.send_manual_reminder(booking, item, db)
    db.commit()
    db.refresh(booking)
    return _row(booking, installment_id, db)


def not_received(*, booking_id: str, installment_id: str, caller_user_id: str, db: Session) -> dict:
    """Undo one step (DECISIONS #33): "client says sent" → not sent, with an
    email to the client; "received" → back to what the client said, or to
    not sent if they never said."""
    vendor, booking, item = _own_booking_payment(booking_id, installment_id, caller_user_id, db)
    row = _row(booking, installment_id, db)
    if row["booking"]["state"] == "cancelled":
        raise LedgerError(400, "This booking is cancelled")
    if item.get("confirmed_at"):
        keep = row["marked_by_client"]
        doc.unconfirm(booking, installment_id, keep_mark=keep)
        doc.record(db, booking, "payment_not_received", "vendor", {
            "installment_id": installment_id, "label": item["label"],
            "amount_cents": item["amount_cents"], "step": "unconfirmed", "kept_mark": keep,
        })
        db.commit()
    elif item.get("marked_paid_at"):
        doc.unmark(booking, installment_id)
        doc.record(db, booking, "payment_not_received", "vendor", {
            "installment_id": installment_id, "label": item["label"],
            "amount_cents": item["amount_cents"], "step": "unmarked",
        })
        db.commit()
        _tell_client_not_received(booking, item, vendor, db)
    else:
        raise LedgerError(400, "It isn't marked as sent or received, so there's nothing to undo")
    db.refresh(booking)
    return _row(booking, installment_id, db)


def _tell_client_not_received(booking: Booking, item: dict, vendor: Vendor, db: Session) -> None:
    """Fixed wording, the payment and its amount. Best effort."""
    if not booking.guest_email:
        return
    vendor_user = db.query(User).filter(User.user_id == vendor.user_id).first()
    who = f"{vendor_user.f_name} {vendor_user.l_name}".strip() if vendor_user else "Your vendor"
    link = f"{WEB_APP_URL}/booking-link?t={booking.contract_token}"
    label = item["label"].lower()
    try:
        send_email(
            to=booking.guest_email,
            subject=f"{who} hasn't received your {label} yet",
            html=(
                f"<p>{escape(who)} hasn't received your {escape(label)} "
                f"(<strong>${item['amount_cents'] / 100:,.2f}</strong>) yet, so it's back to not sent on "
                f'<a href="{link}">your contract</a>.</p>'
                "<p>If you've sent it, check it went to the right account, or reply to them.</p>"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Not-received email failed for booking %s: %s", booking.booking_id, exc)
