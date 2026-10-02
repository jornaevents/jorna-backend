"""Vendor-authenticated side of the "Contracts" flow: a vendor authors a
booking for a client who may never have used Jorna (see
docs/DECISIONS.md #13), plus the Clients CRM rollup and informal Leads.

The client-facing, no-login side (read/fill-details/sign/attest by token)
lives in guest_booking_service.py instead — this file never trusts a
contract_token as identity, only an authenticated vendor's own user_id.
"""
import json
import secrets
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import WEB_APP_URL
from app.db.models import Booking, ContractTemplate, Lead, Service, User, Vendor
from app.models.schemas import BookingStatus, PaymentStatus, RejectionReason
from app.services import contract_document as doc
from app.services.booking_service import HOLDING_CONTRACT_STATUSES, vendor_has_conflicting_booking
from app.services.email_service import send_email
from app.utils.timeutil import utc_iso

# How long a sent contract holds its date when the vendor hasn't set their
# own Vendor.contract_hold_days. docs/DECISIONS.md #15.
DEFAULT_HOLD_DAYS = 7
MAX_HOLD_DAYS = 60


class ContractError(Exception):
    """Raised when a contract/lead operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _token() -> str:
    """A guest booking link's whole credential — unguessable, not derived
    from booking_id. Same approach as guest_service._token() (RSVP), a
    private helper there too, so this mirrors rather than imports it."""
    return secrets.token_urlsafe(24)


def _now() -> datetime:
    """Naive UTC — the DateTime columns are stored without a timezone."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def contract_state(booking: Booking, now: datetime | None = None) -> str | None:
    """The contract's status as a client or vendor should see it: the stored
    one, except a sent/viewed offer past its hold is "expired". Never stored,
    so nothing has to sweep for it and a resend simply moves the deadline."""
    status = booking.contract_status
    if (
        status in HOLDING_CONTRACT_STATUSES
        and booking.hold_expires_at is not None
        and booking.hold_expires_at <= (now or _now())
    ):
        return "expired"
    return status


def _iso(dt: datetime | None) -> str | None:
    return utc_iso(dt)


def _hold_days(vendor: Vendor, override: int | None) -> int:
    days = override if override is not None else (vendor.contract_hold_days or DEFAULT_HOLD_DAYS)
    if not (1 <= days <= MAX_HOLD_DAYS):
        raise ContractError(400, f"A hold must last between 1 and {MAX_HOLD_DAYS} days")
    return days


def _start_hold(booking: Booking, vendor: Vendor, hold_days: int | None) -> None:
    """Send (or resend): the offer goes live and holds the date for the
    vendor's hold window from now. A resend of an opened offer stays
    "viewed" — the client has still seen it."""
    now = _now()
    booking.contract_status = "viewed" if booking.viewed_at else "sent"
    booking.sent_at = now
    booking.hold_expires_at = now + timedelta(days=_hold_days(vendor, hold_days))


def _own_vendor(*, vendor_id: str, caller_user_id: str, db: Session) -> Vendor:
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise ContractError(404, "Vendor not found")
    if vendor.user_id != caller_user_id:
        raise ContractError(403, "You are not authorised to act as this vendor")
    return vendor


def _contract_dict(booking: Booking, service: Service | None, vendor: Vendor, db: Session) -> dict:
    vendor_user = db.query(User).filter(User.user_id == vendor.user_id).first()
    items = booking.line_items or []
    return {
        "booking_id": booking.booking_id,
        "vendor_id": booking.vendor_id,
        "service_id": booking.service_id,
        "service_name": service.name if service else None,
        "date_iso": booking.date_iso,
        "date_end": booking.date_end,
        "time_start": booking.time_start,
        "time_end": booking.time_end,
        "location": booking.location,
        "guest_count": booking.guest_count,
        "amount_cents": booking.amount_cents,
        "line_items": booking.line_items,
        "subtotal_cents": sum(i["total_cents"] for i in items) if items else booking.amount_cents,
        "discount_cents": booking.discount_cents,
        "payment_schedule": doc.schedule_view(booking),
        "terms_clauses": booking.terms_clauses,
        "document_title": booking.document_title,
        "document_layout": booking.document_layout,
        "revision": booking.revision,
        "signed_snapshot_sha256": booking.signed_snapshot_sha256,
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
        "signed_at": _iso(booking.signed_at),
        "status": booking.status,
        "payment_status": booking.payment_status,
        "contract_status": contract_state(booking),
        "sent_at": _iso(booking.sent_at),
        "viewed_at": _iso(booking.viewed_at),
        "hold_expires_at": _iso(booking.hold_expires_at),
        "declined_at": _iso(booking.declined_at),
        "decline_reason": booking.decline_reason,
        "voided_at": _iso(booking.voided_at),
        "contract_token": booking.contract_token,
        "vendor_display_name": _vendor_name(vendor_user),
        # The latest change proposal's status (docs/DECISIONS.md #23). "open"
        # means the client is waiting on an answer: GET …/proposals.
        "proposal_status": _latest_proposal_status(booking, db),
    }


def _latest_proposal_status(booking: Booking, db: Session) -> str | None:
    from app.services.proposal_service import latest_status

    return latest_status(db, booking)


def _vendor_name(vendor_user: User | None) -> str | None:
    return f"{vendor_user.f_name} {vendor_user.l_name}".strip() if vendor_user else None


def _check_dates(date_iso: str, date_end: str | None) -> None:
    """Reject a schedule nobody could mean. A day of slack on "not in the
    past": the server's today is UTC, so a vendor in California writing a
    contract for tonight is already on tomorrow's date here."""
    try:
        start = date.fromisoformat(date_iso)
        end = date.fromisoformat(date_end) if date_end else start
    except ValueError:
        raise ContractError(400, "Dates must be YYYY-MM-DD")
    if start < datetime.now(timezone.utc).date() - timedelta(days=1):
        raise ContractError(400, "The event date is in the past")
    if end < start:
        raise ContractError(400, "The end date is before the start date")


def _legacy_service(vendor: Vendor, service_id: str | None, db: Session) -> Service:
    service = db.query(Service).filter(Service.service_id == service_id).first() if service_id else None
    if not service or service.vendor_id != vendor.vendor_id:
        raise ContractError(404, "Service not found for this vendor")
    # Hidden packages are fine here — a private package offered only by
    # contract is the point of hiding one. Archived ones are retired.
    if service.status == "archived":
        raise ContractError(400, "That package is archived — restore it before using it in a contract")
    return service


def _check_deposit(deposit_percent: int | None) -> None:
    if deposit_percent is not None and not (0 <= deposit_percent <= 100):
        raise ContractError(400, "deposit_percent must be between 0 and 100")


def _link(booking: Booking) -> str:
    """The client's link — the same one the vendor web app shows
    (lib/contractLink.guestBookingLink there)."""
    return f"{WEB_APP_URL}/booking-link?t={booking.contract_token}"


def _email_client(booking: Booking, vendor: Vendor, db: Session) -> None:
    """Send the client their link. Best effort, like every other email here:
    the contract is already sent (and holding) whether or not this lands."""
    vendor_name = _vendor_name(db.query(User).filter(User.user_id == vendor.user_id).first()) or "Your vendor"
    held = (
        f" {vendor_name} is holding the date for you until "
        f"{booking.hold_expires_at.strftime('%B')} {booking.hold_expires_at.day}."
        if booking.hold_expires_at else ""
    )
    send_email(
        to=booking.guest_email,
        subject=f"{vendor_name} sent you a booking to review",
        html=(
            f"<p>{vendor_name} sent you a booking for {booking.date_iso}.{held}</p>"
            f'<p><a href="{_link(booking)}">Review and sign</a> — no account needed.</p>'
            "<p>You'll pay them directly; Jorna doesn't handle the money.</p>"
        ),
    )
    doc.record(db, booking, "emailed", "vendor", {"to": booking.guest_email})


def create_contract(
    *,
    vendor_id: str,
    caller_user_id: str,
    date_iso: str,
    date_end: str | None,
    time_start: str,
    time_end: str,
    db: Session,
    service_id: str | None = None,
    amount_cents: int | None = None,
    deposit_percent: int | None = None,
    cancellation_window_hours: int | None = None,
    overtime_rate_cents: int | None = None,
    addon_rate_cents: int | None = None,
    contract_terms: dict | None = None,
    guest_name: str | None = None,
    guest_email: str | None = None,
    guest_phone: str | None = None,
    guest_count: int | None = None,
    location: str | None = None,
    draft: bool = False,
    hold_days: int | None = None,
    line_items: list | None = None,
    discount_cents: int | None = None,
    payment_schedule: list | None = None,
    terms_clauses: list | None = None,
    email_client: bool = False,
    document_title: str | None = None,
    document_layout: list | None = None,
) -> dict:
    """A vendor authors the whole booking for a client who hasn't shown up
    yet — no user_id, no login. location defaults to "TBD" when the vendor
    doesn't know the venue yet; the client fills it in themselves via the
    public link (guest_booking_service.fill_details).

    Two shapes (docs/DECISIONS.md #16): line_items (+ discount, and usually a
    payment_schedule and terms_clauses) from the builder, or the original
    one service_id + amount_cents (+ deposit_percent). The second is stored
    as a one-line contract so both read the same afterwards.

    status is set to APPROVED immediately, not PENDING — there is no vendor
    accept/decline step in this flow; the vendor is both author and
    approver of their own offer. What decides whether it holds the date is
    contract_status: sent unless draft=True, holding for hold_days (or the
    vendor's default) — see send_contract and docs/DECISIONS.md #15.

    A draft still has to fit the calendar as it stands: there's no point
    writing an offer for a date that's already taken.
    """
    vendor = _own_vendor(vendor_id=vendor_id, caller_user_id=caller_user_id, db=db)

    if line_items:
        items, primary_service_id = doc.normalize_line_items(line_items, vendor_id=vendor.vendor_id, db=db)
        _, total = doc.totals(items, discount_cents)
    else:
        service = _legacy_service(vendor, service_id, db)
        if not amount_cents or amount_cents <= 0:
            raise ContractError(400, "amount_cents must be greater than zero")
        items, primary_service_id, total, discount_cents = doc.single_item(service, amount_cents), service.service_id, amount_cents, None

    schedule = doc.normalize_schedule(payment_schedule, total_cents=total) if payment_schedule else None
    if schedule is None:
        _check_deposit(deposit_percent)
    # The document editor sends its blocks; its terms sections are the
    # clauses. Older builders send clauses alone.
    if document_layout is not None:
        layout, clauses = doc.normalize_layout(document_layout)
    else:
        layout, clauses = None, doc.normalize_clauses(terms_clauses)

    _check_dates(date_iso, date_end)
    guest_email = (guest_email or "").strip() or None
    if email_client and not draft and not guest_email:
        raise ContractError(400, "Add your client's email to send it to them")

    conflict = vendor_has_conflicting_booking(
        vendor_id=vendor.vendor_id, date_iso=date_iso, date_end=date_end,
        time_start=time_start, time_end=time_end, db=db,
    )
    if conflict:
        raise ContractError(409, "You already have a booking that overlaps this date/time")

    booking = Booking(
        user_id=None,
        vendor_id=vendor.vendor_id,
        service_id=primary_service_id,
        date_iso=date_iso,
        date_end=date_end,
        time_start=time_start,
        time_end=time_end,
        # All optional up front: a vendor usually knows who they're quoting
        # (and often where), and without a name every unopened contract in
        # their list looks the same. The client can still correct these
        # before signing (guest_booking_service.fill_details).
        location=(location or "").strip() or "TBD",
        guest_name=(guest_name or "").strip() or None,
        guest_email=guest_email,
        guest_phone=(guest_phone or "").strip() or None,
        guest_count=guest_count,
        status=BookingStatus.APPROVED.value,
        payment_status=PaymentStatus.UNPAID.value,
        amount_cents=total,
        payment_method="manual",
        confirmed_at=datetime.now(timezone.utc),
        line_items=items,
        discount_cents=discount_cents or None,
        payment_schedule=schedule,
        terms_clauses=clauses,
        document_title=(document_title or "").strip()[:200] or None,
        document_layout=layout,
        revision=1,
        deposit_percent=None if schedule else deposit_percent,
        deposit_amount_cents=(
            round(total * deposit_percent / 100) if deposit_percent is not None and not schedule else None
        ),
        cancellation_window_hours=cancellation_window_hours,
        overtime_rate_cents=overtime_rate_cents,
        addon_rate_cents=addon_rate_cents,
        contract_terms=contract_terms,
        contract_token=_token(),
        contract_status="draft",
    )
    doc.sync_legacy_payment_fields(booking)
    if not draft:
        _start_hold(booking, vendor, hold_days)
    db.add(booking)
    db.flush()
    doc.snapshot_revision(db, booking)
    doc.record(db, booking, "created", "vendor", {"draft": draft})
    if not draft:
        doc.record(db, booking, "sent", "vendor", {"hold_expires_at": _iso(booking.hold_expires_at)})
        if email_client:
            _email_client(booking, vendor, db)
    db.commit()
    db.refresh(booking)
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    return _contract_dict(booking, service, vendor, db)


def get_contract(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise ContractError(404, "Contract not found")
    vendor = _own_vendor(vendor_id=booking.vendor_id, caller_user_id=caller_user_id, db=db)
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    result = _contract_dict(booking, service, vendor, db)
    result["timeline"] = doc.timeline(booking, db, result["contract_status"])
    return result


def contract_pdf(*, booking_id: str, caller_user_id: str, db: Session) -> tuple[bytes, str]:
    """The vendor's copy: signed, or the current version marked unsigned."""
    from app.services import pdf_service

    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking or not booking.contract_token:
        raise ContractError(404, "Contract not found")
    _own_vendor(vendor_id=booking.vendor_id, caller_user_id=caller_user_id, db=db)
    return pdf_service.contract_pdf(booking, db)


def update_contract(
    *, booking_id: str, caller_user_id: str, update_data: dict, db: Session, proposal_id: str | None = None,
    proposal_note: str | None = None,
) -> dict:
    """Edit before it's signed. A signed agreement is immutable — the whole
    point of e-signing something is that it stops moving. Every edit bumps
    revision, so a client who opened the old version can't sign it
    (guest_booking_service.sign_contract).

    With proposal_id this is the vendor's Revise answer to the client's
    change proposal (docs/DECISIONS.md #23): the edit is their new version,
    sent to the client with the hold restarted. Without it, an edit made
    while a proposal is open leaves that proposal answering a version that
    no longer exists, so it's closed as superseded."""
    from app.services import proposal_service

    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise ContractError(404, "Contract not found")
    vendor = _own_vendor(vendor_id=booking.vendor_id, caller_user_id=caller_user_id, db=db)
    proposal = proposal_service.open_for_vendor(booking, proposal_id, db) if proposal_id else None
    apply_update(booking, vendor, update_data, db)
    if proposal is not None:
        proposal_service.mark_revised(booking, vendor, proposal, db, note=proposal_note)
    else:
        proposal_service.close_open(db, booking, "superseded", "vendor")
        proposal_service.drop_draft(db, booking, "vendor")
    db.commit()
    db.refresh(booking)
    if proposal is not None:
        proposal_service.email_reply(booking, vendor, proposal, db)
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    return _contract_dict(booking, service, vendor, db)


def apply_update(booking: Booking, vendor: Vendor, update_data: dict, db: Session) -> None:
    """update_contract's checks and changes, without committing — shared
    with accepting a change proposal, which applies the client's terms the
    same way and has to fail as a whole."""
    if booking.signed_at is not None:
        raise ContractError(400, "This contract has already been signed and can no longer be edited")
    if booking.contract_status == "declined":
        raise ContractError(400, "Your client declined this contract, so it can no longer be edited")
    if booking.status == BookingStatus.REJECTED.value:
        raise ContractError(400, "This contract was voided and can no longer be edited")
    # Keep the version the client is reading, before anything changes, if
    # nothing has yet (a contract from before 0068).
    doc.snapshot_revision(db, booking)

    _check_deposit(update_data.get("deposit_percent"))
    if "amount_cents" in update_data and update_data["amount_cents"] <= 0:
        raise ContractError(400, "amount_cents must be greater than zero")

    # A moved date has to clear the same checks a new contract does —
    # otherwise editing was a way around the double-booking guard.
    schedule_fields = ("date_iso", "date_end", "time_start", "time_end")
    if any(f in update_data for f in schedule_fields):
        new = {f: update_data.get(f, getattr(booking, f)) for f in schedule_fields}
        _check_dates(new["date_iso"], new["date_end"])
        conflict = vendor_has_conflicting_booking(
            vendor_id=booking.vendor_id, date_iso=new["date_iso"], date_end=new["date_end"],
            time_start=new["time_start"], time_end=new["time_end"],
            exclude_booking_id=booking.booking_id, db=db,
        )
        if conflict:
            raise ContractError(409, "You already have a booking that overlaps this date/time")

    # ── What's being sold, and for how much ──
    total_before = booking.amount_cents
    if "line_items" in update_data or "discount_cents" in update_data:
        if "line_items" in update_data:
            items, primary = doc.normalize_line_items(update_data["line_items"], vendor_id=vendor.vendor_id, db=db)
        else:
            items, primary = booking.line_items, booking.service_id
        discount = update_data.get("discount_cents", booking.discount_cents)
        _, total = doc.totals(items, discount)
        booking.line_items, booking.service_id = items, primary
        booking.discount_cents, booking.amount_cents = discount or None, total
    elif "amount_cents" in update_data:
        # The one-number edit older clients send. Fine on a one-line
        # contract; with several lines it's ambiguous which one changed.
        items = booking.line_items or []
        if len(items) > 1 or (items and items[0]["quantity"] != 1) or booking.discount_cents:
            raise ContractError(400, "This contract has several line items — edit those instead of the total")
        booking.amount_cents = update_data["amount_cents"]
        if items:
            booking.line_items = [{**items[0], "unit_price_cents": booking.amount_cents, "total_cents": booking.amount_cents}]

    if "payment_schedule" in update_data:
        booking.payment_schedule = doc.normalize_schedule(update_data["payment_schedule"], total_cents=booking.amount_cents)
        doc.sync_legacy_payment_fields(booking)
    elif booking.payment_schedule and booking.amount_cents != total_before:
        raise ContractError(400, "The total changed — update the payment schedule to match it")
    if "deposit_percent" in update_data and booking.payment_schedule:
        raise ContractError(400, "This contract uses a payment schedule — edit that instead of the deposit")

    for field in (
        "date_iso", "date_end", "time_start", "time_end", "deposit_percent",
        "cancellation_window_hours", "overtime_rate_cents", "addon_rate_cents",
        "contract_terms", "guest_count",
    ):
        if field in update_data:
            setattr(booking, field, update_data[field])
    for field in ("guest_name", "guest_email", "guest_phone"):
        if field in update_data:
            setattr(booking, field, (update_data[field] or "").strip() or None)
    if "location" in update_data:
        booking.location = (update_data["location"] or "").strip() or "TBD"
    if "terms_clauses" in update_data:
        booking.terms_clauses = doc.normalize_clauses(update_data["terms_clauses"])
        if "document_layout" not in update_data:
            # Clauses edited on their own (a change proposal): keep the
            # editor's layout in step, or its terms blocks point at nothing.
            booking.document_layout, booking.terms_clauses = doc.relayout(booking.document_layout, booking.terms_clauses)
    if "document_layout" in update_data:
        booking.document_layout, booking.terms_clauses = doc.normalize_layout(update_data["document_layout"])
    if "document_title" in update_data:
        booking.document_title = (update_data["document_title"] or "").strip()[:200] or None

    # Re-derive the snapshot rather than trust a client-supplied deposit_amount_cents.
    if not booking.payment_schedule and ("deposit_percent" in update_data or booking.amount_cents != total_before):
        booking.deposit_amount_cents = (
            round(booking.amount_cents * booking.deposit_percent / 100)
            if booking.deposit_percent is not None
            else None
        )

    booking.revision = (booking.revision or 1) + 1
    doc.snapshot_revision(db, booking)
    doc.record(db, booking, "edited", "vendor", {"revision": booking.revision, "fields": sorted(update_data)})


def resend(booking: Booking, vendor: Vendor, db: Session) -> None:
    """Put a new version back in front of the client: the hold restarts from
    now, as a resend does. The caller has already checked the date (an edit
    that moves it runs the overlap check)."""
    _start_hold(booking, vendor, None)
    doc.record(db, booking, "resent", "vendor", {"hold_expires_at": _iso(booking.hold_expires_at)})


def void_contract(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    """Withdraw an unsigned contract. Until this existed an unsent or
    abandoned link held its date forever — the booking is created APPROVED,
    which is what the double-booking guard counts — with no way to let it go.

    Recorded the way a vendor withdrawing any accepted booking is (REJECTED +
    VENDOR_WITHDREW), so every "is this booking live" rule, on both clients
    and here, already treats it as dead. Signed contracts are out of scope:
    releasing a client from a signed agreement is a cancellation with a
    policy attached, not an undo. Idempotent — voiding twice is a no-op.
    """
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking or not booking.contract_token:
        raise ContractError(404, "Contract not found")
    vendor = _own_vendor(vendor_id=booking.vendor_id, caller_user_id=caller_user_id, db=db)
    if booking.signed_at is not None:
        raise ContractError(400, "This contract has already been signed, so it can't be voided")
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    if booking.status == BookingStatus.REJECTED.value:
        return _contract_dict(booking, service, vendor, db)

    from app.services.proposal_service import close_open

    booking.status = BookingStatus.REJECTED.value
    booking.rejected_reason = RejectionReason.VENDOR_WITHDREW.value
    booking.contract_status = "voided"
    booking.voided_at = _now()
    doc.record(db, booking, "voided", "vendor")
    close_open(db, booking, "superseded", "vendor")
    db.commit()
    db.refresh(booking)
    result = _contract_dict(booking, service, vendor, db)

    # Only someone who'd already given an email — a client who never opened
    # the link has nothing to be told.
    if booking.guest_email:
        who = result["vendor_display_name"] or "Your vendor"
        send_email(
            to=booking.guest_email,
            subject=f"{who} withdrew a booking offer",
            html=(
                f"<p>{who} withdrew the booking offer for "
                f"<strong>{result['service_name'] or 'their service'}</strong> on "
                f"{booking.date_iso}. The link you were sent no longer works, and "
                "nothing is owed.</p>"
            ),
        )
    return result


def send_contract(
    *, booking_id: str, caller_user_id: str, db: Session,
    hold_days: int | None = None, email_client: bool = False,
) -> dict:
    """Send a draft, or resend an offer — including one whose hold lapsed —
    restarting the hold from now. The date has to still be free: a lapsed
    hold let someone else take it, and that booking wins.

    email_client also emails the client their link; otherwise the vendor
    shares it themselves. Signed, declined and voided contracts can't be sent.
    """
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking or not booking.contract_token:
        raise ContractError(404, "Contract not found")
    vendor = _own_vendor(vendor_id=booking.vendor_id, caller_user_id=caller_user_id, db=db)
    if booking.signed_at is not None:
        raise ContractError(400, "This contract has already been signed")
    if booking.contract_status == "declined":
        raise ContractError(400, "Your client declined this contract — start a new one to offer again")
    if booking.status == BookingStatus.REJECTED.value:
        raise ContractError(400, "This contract was voided and can't be sent")
    if email_client and not booking.guest_email:
        raise ContractError(400, "Add your client's email to send it to them")

    _check_dates(booking.date_iso, booking.date_end)
    conflict = vendor_has_conflicting_booking(
        vendor_id=booking.vendor_id, date_iso=booking.date_iso, date_end=booking.date_end,
        time_start=booking.time_start, time_end=booking.time_end,
        exclude_booking_id=booking.booking_id, db=db,
    )
    if conflict:
        raise ContractError(409, "That date is booked now — move this contract to another date first")

    resend = booking.sent_at is not None
    _start_hold(booking, vendor, hold_days)
    doc.record(db, booking, "resent" if resend else "sent", "vendor", {"hold_expires_at": _iso(booking.hold_expires_at)})
    if email_client:
        _email_client(booking, vendor, db)
    db.commit()
    db.refresh(booking)
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    return _contract_dict(booking, service, vendor, db)


def confirm_installment(*, booking_id: str, installment_id: str, caller_user_id: str, db: Session) -> dict:
    """The vendor says one scheduled payment arrived — whether or not the
    client marked it sent."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking or not booking.contract_token:
        raise ContractError(404, "Contract not found")
    vendor = _own_vendor(vendor_id=booking.vendor_id, caller_user_id=caller_user_id, db=db)
    if booking.signed_at is None:
        raise ContractError(400, "This contract hasn't been signed yet")
    item = doc.installment(booking, installment_id)
    if not doc.confirm_received(booking, [installment_id]):
        raise ContractError(400, f"“{item['label']}” is already confirmed")
    doc.record(db, booking, "payment_confirmed", "vendor", {
        "installment_id": installment_id, "label": item["label"], "amount_cents": item["amount_cents"],
    })
    db.commit()
    db.refresh(booking)
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    return _contract_dict(booking, service, vendor, db)


# ── Marketplace requests become proposals (Phase 4, DECISIONS #17) ──

def _vendor_terms(vendor: Vendor, service: Service | None) -> dict:
    """The terms a vendor usually offers: the package's own where it sets
    them, otherwise the vendor-wide defaults."""
    pick = lambda own, default: own if own is not None else default  # noqa: E731
    return {
        "deposit_percent": pick(service.deposit_percent if service else None, vendor.default_deposit_percent),
        "cancellation_window_hours": pick(
            service.cancellation_window_hours if service else None, vendor.default_cancellation_window_hours,
        ),
        "overtime_rate_cents": pick(service.overtime_rate_cents if service else None, vendor.default_overtime_rate_cents),
    }


def _default_clauses(vendor: Vendor) -> list[dict] | None:
    terms = vendor.default_contract_terms or {}
    raw = []
    if terms.get("equipment_power"):
        raw.append({"key": "equipment_power", "title": "Equipment & power", "body": terms["equipment_power"]})
    if terms.get("travel"):
        raw.append({"key": "travel", "title": "Travel", "body": terms["travel"]})
    for c in terms.get("custom") or []:
        if c.get("label") and c.get("value"):
            raw.append({"title": c["label"], "body": c["value"]})
    return doc.normalize_clauses(raw)


def _default_schedule(total: int, deposit_percent: int | None) -> list[dict]:
    """Deposit on signing and the rest two weeks out when the vendor takes a
    deposit; otherwise the whole amount on signing."""
    if deposit_percent and 0 < deposit_percent < 100:
        deposit = round(total * deposit_percent / 100)
        raw = [
            {"label": "Deposit", "amount_cents": deposit, "due_type": "on_signing"},
            {"label": "Final balance", "amount_cents": total - deposit, "due_type": "before_event", "due_days": 14},
        ]
    else:
        raw = [{"label": "Payment in full", "amount_cents": total, "due_type": "on_signing"}]
    return doc.normalize_schedule(raw, total_cents=total)


def attach_proposal(
    booking: Booking,
    vendor: Vendor,
    db: Session,
    *,
    line_items: list | None = None,
    discount_cents: int | None = None,
    payment_schedule: list | None = None,
    terms_clauses: list | None = None,
    cancellation_window_hours: int | None = None,
    overtime_rate_cents: int | None = None,
    hold_days: int | None = None,
    document_title: str | None = None,
    document_layout: list | None = None,
) -> None:
    """Turn an accepted marketplace request into a sent proposal, in place —
    the same row, so its messages, bundle and event stay attached. Without a
    document it's built from the request and the vendor's usual terms, which
    is what a plain "Accept" (web Bookings page, iOS) gets.

    Doesn't commit or email; the caller does both once the rest of the
    acceptance has gone through.
    """
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    usual = _vendor_terms(vendor, service)
    if line_items:
        items, primary = doc.normalize_line_items(line_items, vendor_id=vendor.vendor_id, db=db)
        _, total = doc.totals(items, discount_cents)
    else:
        from app.services.booking_service import resolve_total_cents

        total = resolve_total_cents(booking, service)
        if not total or total <= 0:
            raise ContractError(
                409,
                "This request doesn't have a total yet — the client hasn't said how many "
                "guests or hours. Accept it from the contract builder and set the price.",
            )
        items, primary, discount_cents = doc.single_item(service, total), service.service_id, None

    booking.line_items = items
    booking.service_id = primary
    booking.amount_cents = total
    booking.discount_cents = discount_cents or None
    booking.payment_schedule = (
        doc.normalize_schedule(payment_schedule, total_cents=total)
        if payment_schedule
        else _default_schedule(total, usual["deposit_percent"])
    )
    doc.sync_legacy_payment_fields(booking)
    if document_layout is not None:
        booking.document_layout, booking.terms_clauses = doc.normalize_layout(document_layout)
    else:
        booking.terms_clauses = (
            doc.normalize_clauses(terms_clauses) if terms_clauses is not None else _default_clauses(vendor)
        )
    booking.document_title = (document_title or "").strip()[:200] or None
    booking.cancellation_window_hours = (
        cancellation_window_hours if cancellation_window_hours is not None else usual["cancellation_window_hours"]
    )
    booking.overtime_rate_cents = (
        overtime_rate_cents if overtime_rate_cents is not None else usual["overtime_rate_cents"]
    )
    booking.revision = 1

    # Who it's for comes from the client's account; the sign page reads and
    # lets them correct these, as it does for anyone.
    client = db.query(User).filter(User.user_id == booking.user_id).first()
    if client:
        booking.guest_name = booking.guest_name or f"{client.f_name} {client.l_name}".strip() or None
        booking.guest_email = booking.guest_email or client.email
        booking.guest_phone = booking.guest_phone or client.phone

    booking.contract_token = booking.contract_token or _token()
    booking.status = BookingStatus.APPROVED.value
    booking.confirmed_at = datetime.now(timezone.utc)
    _start_hold(booking, vendor, hold_days)
    doc.snapshot_revision(db, booking)
    doc.record(db, booking, "created", "vendor", {"from_request": True})
    doc.record(db, booking, "sent", "vendor", {"hold_expires_at": _iso(booking.hold_expires_at)})


def send_proposal_email(booking: Booking, vendor: Vendor, db: Session) -> None:
    """After the acceptance commits: the client's link, to their account's
    email. Records the event, so it commits its own row."""
    if booking.guest_email:
        _email_client(booking, vendor, db)
        db.commit()


def propose_from_request(
    *, booking_id: str, caller_user_id: str, db: Session, email_client: bool = True, **document,
) -> dict:
    """The vendor accepts a marketplace request with a proposal they've
    written (the builder, ?request=). Same checks as a plain accept."""
    from app.services.booking_service import BookingError, check_can_accept

    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking or booking.user_id is None:
        raise ContractError(404, "Request not found")
    vendor = _own_vendor(vendor_id=booking.vendor_id, caller_user_id=caller_user_id, db=db)
    if booking.contract_token:
        raise ContractError(400, "This request already has a contract — edit that instead")
    try:
        check_can_accept(booking, db)
    except BookingError as e:
        raise ContractError(e.status_code, e.detail)

    attach_proposal(booking, vendor, db, **document)
    db.commit()
    db.refresh(booking)
    if email_client:
        send_proposal_email(booking, vendor, db)
        db.refresh(booking)

    from app.services.calendar_service import sync_booking_to_calendar

    sync_booking_to_calendar(booking, db)
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    return _contract_dict(booking, service, vendor, db)


# ── Templates ────────────────────────────────────────────────────────

MAX_TEMPLATES = 50
MAX_TEMPLATE_BYTES = 64_000


def _template_dict(t: ContractTemplate) -> dict:
    return {
        "template_id": t.template_id,
        "name": t.name,
        "kind": t.kind or "agreement",
        "body": t.body,
        "created_at": _iso(t.created_at),
        "updated_at": _iso(t.updated_at),
    }


TEMPLATE_KINDS = ("agreement", "addendum", "cancellation")


def _check_template(name: str | None, body: dict | None, kind: str | None = None) -> None:
    if kind is not None and kind not in TEMPLATE_KINDS:
        raise ContractError(400, f"A template is one of: {', '.join(TEMPLATE_KINDS)}")
    if name is not None and not name.strip():
        raise ContractError(400, "Give the template a name")
    if body is not None:
        if not isinstance(body, dict):
            raise ContractError(400, "A template body is an object")
        if len(json.dumps(body)) > MAX_TEMPLATE_BYTES:
            raise ContractError(400, "That template is too large")


def list_templates(*, vendor_id: str, caller_user_id: str, db: Session) -> dict:
    _own_vendor(vendor_id=vendor_id, caller_user_id=caller_user_id, db=db)
    rows = (
        db.query(ContractTemplate)
        .filter(ContractTemplate.vendor_id == vendor_id)
        .order_by(ContractTemplate.name)
        .all()
    )
    return {"items": [_template_dict(t) for t in rows], "total": len(rows)}


def create_template(
    *, vendor_id: str, caller_user_id: str, name: str, body: dict, db: Session, kind: str = "agreement",
) -> dict:
    _own_vendor(vendor_id=vendor_id, caller_user_id=caller_user_id, db=db)
    _check_template(name, body, kind)
    if db.query(ContractTemplate).filter(ContractTemplate.vendor_id == vendor_id).count() >= MAX_TEMPLATES:
        raise ContractError(400, f"You can keep up to {MAX_TEMPLATES} templates — delete one first")
    now = _now()
    t = ContractTemplate(
        vendor_id=vendor_id, name=name.strip()[:120], body=body, kind=kind, created_at=now, updated_at=now,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return _template_dict(t)


def _own_template(template_id: str, caller_user_id: str, db: Session) -> ContractTemplate:
    t = db.query(ContractTemplate).filter(ContractTemplate.template_id == template_id).first()
    if not t:
        raise ContractError(404, "Template not found")
    _own_vendor(vendor_id=t.vendor_id, caller_user_id=caller_user_id, db=db)
    return t


def update_template(
    *, template_id: str, caller_user_id: str, name: str | None, body: dict | None, db: Session,
    kind: str | None = None,
) -> dict:
    t = _own_template(template_id, caller_user_id, db)
    _check_template(name, body, kind)
    if kind is not None:
        t.kind = kind
    if name is not None:
        t.name = name.strip()[:120]
    if body is not None:
        t.body = body
    t.updated_at = _now()
    db.commit()
    db.refresh(t)
    return _template_dict(t)


def delete_template(*, template_id: str, caller_user_id: str, db: Session) -> dict:
    t = _own_template(template_id, caller_user_id, db)
    db.delete(t)
    db.commit()
    return {"message": "Template deleted"}


# ── Clients CRM ──────────────────────────────────────────────────────

_DEAD_STATUSES = (BookingStatus.REJECTED.value,)


def get_vendor_clients(*, vendor_id: str, caller_user_id: str, db: Session) -> dict:
    """Group a vendor's own bookings by client. A real account groups by
    user_id; a guest booking has none, so it groups by (guest_name,
    guest_phone) instead — an approximation, not identity resolution, but
    good enough for a vendor's own CRM list."""
    _own_vendor(vendor_id=vendor_id, caller_user_id=caller_user_id, db=db)

    bookings = (
        db.query(Booking)
        .filter(Booking.vendor_id == vendor_id, Booking.status.notin_(_DEAD_STATUSES))
        .all()
    )
    client_user_ids = {b.user_id for b in bookings if b.user_id is not None}
    users = {
        u.user_id: u
        for u in db.query(User).filter(User.user_id.in_(client_user_ids)).all()
    } if client_user_ids else {}

    groups: dict[tuple, dict] = {}
    for b in bookings:
        if b.user_id is not None:
            key = ("user", b.user_id)
        else:
            key = ("guest", (b.guest_name or "").strip().lower(), (b.guest_phone or "").strip())

        g = groups.get(key)
        if not g:
            g = {
                "key": "|".join(str(k) for k in key),
                "user_id": b.user_id,
                "name": None,
                "email": b.guest_email,
                "phone": b.guest_phone,
                "event_count": 0,
                "lifetime_value_cents": 0,
                "is_guest": b.user_id is None,
            }
            groups[key] = g

        g["event_count"] += 1
        g["lifetime_value_cents"] += b.amount_cents or 0
        client_user = users.get(b.user_id) if b.user_id is not None else None
        if client_user is not None and g["name"] is None:
            g["name"] = f"{client_user.f_name} {client_user.l_name}".strip()
            g["email"] = client_user.email
            g["phone"] = client_user.phone
        elif b.user_id is None and g["name"] is None and b.guest_name:
            g["name"] = b.guest_name

    clients = sorted(groups.values(), key=lambda g: g["lifetime_value_cents"], reverse=True)
    for c in clients:
        c["repeat_client"] = c["event_count"] > 1

    return {"items": clients, "total": len(clients)}


# ── Leads ────────────────────────────────────────────────────────────

def _lead_dict(lead: Lead) -> dict:
    return {
        "lead_id": lead.lead_id,
        "vendor_id": lead.vendor_id,
        "name": lead.name,
        "phone": lead.phone,
        "email": lead.email,
        "event_date_iso": lead.event_date_iso,
        "note": lead.note,
        "status": lead.status,
        "converted_booking_id": lead.converted_booking_id,
        "created_at": lead.created_at.isoformat(),
        "updated_at": lead.updated_at.isoformat(),
        "archived_at": utc_iso(lead.archived_at),
        "user_id": lead.user_id,
        "conversation_id": lead.conversation_id,
    }


def create_lead(
    *, vendor_id: str, caller_user_id: str, name: str, phone: str | None,
    email: str | None, event_date_iso: str | None, note: str | None, db: Session,
) -> dict:
    _own_vendor(vendor_id=vendor_id, caller_user_id=caller_user_id, db=db)
    if not name.strip():
        raise ContractError(400, "name is required")
    now = datetime.now(timezone.utc)
    lead = Lead(
        vendor_id=vendor_id, name=name.strip(), phone=phone, email=email,
        event_date_iso=event_date_iso, note=note, status="new",
        created_at=now, updated_at=now,
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return _lead_dict(lead)


def list_leads(*, vendor_id: str, caller_user_id: str, db: Session) -> dict:
    _own_vendor(vendor_id=vendor_id, caller_user_id=caller_user_id, db=db)
    leads = (
        db.query(Lead)
        .filter(Lead.vendor_id == vendor_id)
        .order_by(Lead.created_at.desc())
        .all()
    )
    return {"items": [_lead_dict(l) for l in leads], "total": len(leads)}


def _own_lead(*, lead_id: str, caller_user_id: str, db: Session) -> Lead:
    lead = db.query(Lead).filter(Lead.lead_id == lead_id).first()
    if not lead:
        raise ContractError(404, "Lead not found")
    _own_vendor(vendor_id=lead.vendor_id, caller_user_id=caller_user_id, db=db)
    return lead


def update_lead(*, lead_id: str, caller_user_id: str, update_data: dict, db: Session) -> dict:
    lead = _own_lead(lead_id=lead_id, caller_user_id=caller_user_id, db=db)
    for field in ("name", "phone", "email", "event_date_iso", "note", "status"):
        if field in update_data:
            setattr(lead, field, update_data[field])
    # Archiving hides a lead from the active list; it isn't a status, so it
    # doesn't touch `status` and unarchiving restores it as it was.
    if "archived" in update_data:
        lead.archived_at = _now() if update_data["archived"] else None
    lead.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(lead)
    return _lead_dict(lead)


def delete_lead(*, lead_id: str, caller_user_id: str, db: Session) -> dict:
    lead = _own_lead(lead_id=lead_id, caller_user_id=caller_user_id, db=db)
    db.delete(lead)
    db.commit()
    return {"message": "Lead deleted"}


def convert_lead(*, lead_id: str, caller_user_id: str, db: Session, **contract_fields) -> dict:
    """Turn a lead into a real contract/booking — create_contract's
    arguments, minus the vendor. The lead row is kept afterward (not
    deleted) as the vendor's own record of how this client was won — see
    converted_booking_id."""
    lead = _own_lead(lead_id=lead_id, caller_user_id=caller_user_id, db=db)
    if lead.converted_booking_id:
        raise ContractError(400, "This lead has already been converted")

    # The lead fills whatever the vendor left blank on the form; anything
    # they typed there is the newer, deliberate answer. Filled in before
    # creating, so "email it to them" can use the lead's email.
    for field, fallback in (("guest_name", lead.name), ("guest_phone", lead.phone), ("guest_email", lead.email)):
        contract_fields[field] = (contract_fields.get(field) or "").strip() or fallback

    contract = create_contract(
        vendor_id=lead.vendor_id, caller_user_id=caller_user_id, db=db, **contract_fields,
    )
    lead.converted_booking_id = contract["booking_id"]
    lead.status = "won"
    lead.updated_at = datetime.now(timezone.utc)
    db.commit()
    return contract
