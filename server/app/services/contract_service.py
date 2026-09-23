"""Vendor-authenticated side of the "Contracts" flow: a vendor authors a
booking for a client who may never have used Jorna (see
docs/DECISIONS.md #13), plus the Clients CRM rollup and informal Leads.

The client-facing, no-login side (read/fill-details/sign/attest by token)
lives in guest_booking_service.py instead — this file never trusts a
contract_token as identity, only an authenticated vendor's own user_id.
"""
import secrets
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.db.models import Booking, Lead, Service, User, Vendor
from app.models.schemas import BookingStatus, PaymentStatus, RejectionReason
from app.services.booking_service import vendor_has_conflicting_booking
from app.services.email_service import send_email


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


def _own_vendor(*, vendor_id: str, caller_user_id: str, db: Session) -> Vendor:
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise ContractError(404, "Vendor not found")
    if vendor.user_id != caller_user_id:
        raise ContractError(403, "You are not authorised to act as this vendor")
    return vendor


def _contract_dict(booking: Booking, service: Service | None, vendor: Vendor, db: Session) -> dict:
    vendor_user = db.query(User).filter(User.user_id == vendor.user_id).first()
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
        "status": booking.status,
        "contract_token": booking.contract_token,
        "vendor_display_name": f"{vendor_user.f_name} {vendor_user.l_name}".strip()
        if vendor_user
        else None,
    }


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


def create_contract(
    *,
    vendor_id: str,
    caller_user_id: str,
    service_id: str,
    date_iso: str,
    date_end: str | None,
    time_start: str,
    time_end: str,
    amount_cents: int,
    deposit_percent: int | None,
    cancellation_window_hours: int | None,
    overtime_rate_cents: int | None,
    addon_rate_cents: int | None,
    contract_terms: dict | None,
    db: Session,
    guest_name: str | None = None,
    guest_email: str | None = None,
    guest_phone: str | None = None,
    location: str | None = None,
) -> dict:
    """A vendor authors the whole booking for a client who hasn't shown up
    yet — no user_id, no login. location defaults to "TBD" when the vendor
    doesn't know the venue yet; the client fills it in themselves via the
    public link (guest_booking_service.fill_details).

    status is set to APPROVED immediately, not PENDING — there is no vendor
    accept/decline step in this flow; the vendor is both author and
    approver of their own offer. Signing later only sets signer_name/
    signed_at, it doesn't change status again.
    """
    vendor = _own_vendor(vendor_id=vendor_id, caller_user_id=caller_user_id, db=db)

    service = db.query(Service).filter(Service.service_id == service_id).first()
    if not service or service.vendor_id != vendor.vendor_id:
        raise ContractError(404, "Service not found for this vendor")

    if amount_cents <= 0:
        raise ContractError(400, "amount_cents must be greater than zero")

    _check_dates(date_iso, date_end)

    deposit_amount_cents = None
    if deposit_percent is not None:
        if not (0 <= deposit_percent <= 100):
            raise ContractError(400, "deposit_percent must be between 0 and 100")
        deposit_amount_cents = round(amount_cents * deposit_percent / 100)

    conflict = vendor_has_conflicting_booking(
        vendor_id=vendor.vendor_id, date_iso=date_iso, date_end=date_end,
        time_start=time_start, time_end=time_end, db=db,
    )
    if conflict:
        raise ContractError(409, "You already have a booking that overlaps this date/time")

    booking = Booking(
        user_id=None,
        vendor_id=vendor.vendor_id,
        service_id=service_id,
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
        guest_email=(guest_email or "").strip() or None,
        guest_phone=(guest_phone or "").strip() or None,
        status=BookingStatus.APPROVED.value,
        payment_status=PaymentStatus.UNPAID.value,
        amount_cents=amount_cents,
        payment_method="manual",
        confirmed_at=datetime.now(timezone.utc),
        deposit_percent=deposit_percent,
        deposit_amount_cents=deposit_amount_cents,
        cancellation_window_hours=cancellation_window_hours,
        overtime_rate_cents=overtime_rate_cents,
        addon_rate_cents=addon_rate_cents,
        contract_terms=contract_terms,
        contract_token=_token(),
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    return _contract_dict(booking, service, vendor, db)


def get_contract(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise ContractError(404, "Contract not found")
    vendor = _own_vendor(vendor_id=booking.vendor_id, caller_user_id=caller_user_id, db=db)
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    return _contract_dict(booking, service, vendor, db)


def update_contract(*, booking_id: str, caller_user_id: str, update_data: dict, db: Session) -> dict:
    """Edit terms before it's sent/signed. A signed agreement is immutable —
    the whole point of e-signing something is that it stops moving."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise ContractError(404, "Contract not found")
    vendor = _own_vendor(vendor_id=booking.vendor_id, caller_user_id=caller_user_id, db=db)
    if booking.signed_at is not None:
        raise ContractError(400, "This contract has already been signed and can no longer be edited")
    if booking.status == BookingStatus.REJECTED.value:
        raise ContractError(400, "This contract was voided and can no longer be edited")

    if "deposit_percent" in update_data and update_data["deposit_percent"] is not None:
        if not (0 <= update_data["deposit_percent"] <= 100):
            raise ContractError(400, "deposit_percent must be between 0 and 100")
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

    for field in (
        "date_iso", "date_end", "time_start", "time_end", "amount_cents",
        "deposit_percent", "cancellation_window_hours", "overtime_rate_cents",
        "addon_rate_cents", "contract_terms",
    ):
        if field in update_data:
            setattr(booking, field, update_data[field])
    for field in ("guest_name", "guest_email", "guest_phone"):
        if field in update_data:
            setattr(booking, field, (update_data[field] or "").strip() or None)
    if "location" in update_data:
        booking.location = (update_data["location"] or "").strip() or "TBD"

    # Re-derive the snapshot rather than trust a client-supplied deposit_amount_cents.
    if "deposit_percent" in update_data or "amount_cents" in update_data:
        booking.deposit_amount_cents = (
            round(booking.amount_cents * booking.deposit_percent / 100)
            if booking.deposit_percent is not None
            else None
        )

    db.commit()
    db.refresh(booking)
    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    return _contract_dict(booking, service, vendor, db)


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

    booking.status = BookingStatus.REJECTED.value
    booking.rejected_reason = RejectionReason.VENDOR_WITHDREW.value
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
    lead.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(lead)
    return _lead_dict(lead)


def delete_lead(*, lead_id: str, caller_user_id: str, db: Session) -> dict:
    lead = _own_lead(lead_id=lead_id, caller_user_id=caller_user_id, db=db)
    db.delete(lead)
    db.commit()
    return {"message": "Lead deleted"}


def convert_lead(
    *,
    lead_id: str,
    caller_user_id: str,
    service_id: str,
    date_iso: str,
    date_end: str | None,
    time_start: str,
    time_end: str,
    amount_cents: int,
    deposit_percent: int | None,
    cancellation_window_hours: int | None,
    overtime_rate_cents: int | None,
    addon_rate_cents: int | None,
    contract_terms: dict | None,
    db: Session,
    guest_name: str | None = None,
    guest_email: str | None = None,
    guest_phone: str | None = None,
    location: str | None = None,
) -> dict:
    """Turn a lead into a real contract/booking. The lead row is kept
    afterward (not deleted) as the vendor's own record of how this client
    was won — see converted_booking_id."""
    lead = _own_lead(lead_id=lead_id, caller_user_id=caller_user_id, db=db)
    if lead.converted_booking_id:
        raise ContractError(400, "This lead has already been converted")

    vendor = db.query(Vendor).filter(Vendor.vendor_id == lead.vendor_id).first()
    contract = create_contract(
        vendor_id=vendor.vendor_id, caller_user_id=caller_user_id, service_id=service_id,
        date_iso=date_iso, date_end=date_end, time_start=time_start, time_end=time_end,
        amount_cents=amount_cents, deposit_percent=deposit_percent,
        cancellation_window_hours=cancellation_window_hours,
        overtime_rate_cents=overtime_rate_cents, addon_rate_cents=addon_rate_cents,
        contract_terms=contract_terms, db=db,
        guest_name=guest_name, guest_email=guest_email, guest_phone=guest_phone,
        location=location,
    )

    # The lead fills whatever the vendor left blank on the form; anything
    # they typed there is the newer, deliberate answer.
    booking = db.query(Booking).filter(Booking.booking_id == contract["booking_id"]).first()
    booking.guest_name = booking.guest_name or lead.name
    booking.guest_phone = booking.guest_phone or lead.phone
    booking.guest_email = booking.guest_email or lead.email

    lead.converted_booking_id = booking.booking_id
    lead.status = "won"
    lead.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(booking)

    service = db.query(Service).filter(Service.service_id == service_id).first()
    return _contract_dict(booking, service, vendor, db)
