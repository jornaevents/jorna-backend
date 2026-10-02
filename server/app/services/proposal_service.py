"""Change proposals on an unsigned contract — docs/DECISIONS.md #23.

While a contract is out for signature, the client can suggest edits to any
of its terms: the event details, line items, payment schedule, policies and
clause text. The vendor answers one of three ways:

- **Accept** — the client's terms become the next revision, sent back to
  them with the hold restarted.
- **Decline** — the current version stands, with the vendor's note.
- **Revise** — the vendor edits the contract themselves (PATCH
  /contracts/{id} with proposal_id) and that version goes back instead.

The contract is always the vendor's position: there's no counter-proposal,
and the client signs a version the vendor sent. One proposal is open at a
time. A new one replaces it, and so does anything that changes the version
it was based on (a vendor edit, signing, voiding).

The client side is reached by the contract's token, like signing; the
vendor side by an authenticated vendor who owns the contract.
"""
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Booking, ContractProposal, ContractRevision, Service, User, Vendor
from app.services import contract_document as doc
from app.services.contract_service import (
    ContractError,
    _check_dates,
    _link,
    _own_vendor,
    _vendor_name,
    apply_update,
    get_contract,
    resend,
)
from app.services.email_service import send_email
from app.utils.timeutil import utc_iso

OPEN = "open"
MAX_MESSAGE = 2000
MAX_NOTE = 1000
MAX_LISTED = 20
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ProposalError(ContractError):
    """A change proposal can't be made or answered. A ContractError, so the
    vendor's contract routes report it like any other."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _text(raw, limit: int) -> str | None:
    return (raw or "").strip()[:limit] or None if isinstance(raw, str) or raw is None else str(raw)[:limit]


def summary(p: ContractProposal) -> dict:
    return {
        "proposal_id": p.proposal_id,
        "base_revision": p.base_revision,
        "status": p.status,
        "message": p.message,
        "response_note": p.response_note,
        "result_revision": p.result_revision,
        "created_at": utc_iso(p.created_at),
        "responded_at": utc_iso(p.responded_at),
    }


def _dict(p: ContractProposal) -> dict:
    return {**summary(p), "proposed": p.proposed}


def _open(db: Session, booking_id: str) -> ContractProposal | None:
    return db.query(ContractProposal).filter(
        ContractProposal.booking_id == booking_id, ContractProposal.status == OPEN,
    ).first()


def latest_for(db: Session, booking_ids: list[str]) -> dict[str, ContractProposal]:
    """booking_id → its most recent proposal, in one query — for lists."""
    if not booking_ids:
        return {}
    out: dict[str, ContractProposal] = {}
    rows = (
        db.query(ContractProposal)
        .filter(ContractProposal.booking_id.in_(booking_ids))
        .order_by(ContractProposal.created_at)
        .all()
    )
    for p in rows:
        out[p.booking_id] = p
    return out


def latest_status(db: Session, booking: Booking) -> str | None:
    """The most recent proposal's status, on a contract that can still
    change. Null otherwise, which is the ordinary case."""
    if not booking.contract_token or booking.signed_at is not None:
        return None
    p = latest_for(db, [booking.booking_id]).get(booking.booking_id)
    return p.status if p else None


def changed_fields(base: dict, proposed: dict) -> list[str]:
    return [f for f in doc.TERMS_FIELDS if base.get(f) != proposed.get(f)]


def close_open(
    db: Session, booking: Booking, status: str, actor: str, note: str | None = None,
) -> ContractProposal | None:
    """End the open proposal, if any, without answering it — replaced by a
    newer one, withdrawn by signing, or overtaken by a vendor edit."""
    p = _open(db, booking.booking_id)
    if p is not None:
        p.status = status
        p.responded_at = _now()
        p.response_note = note
        doc.record(db, booking, f"proposal_{status}", actor, {"proposal_id": p.proposal_id})
    return p


# ── What the client proposed ─────────────────────────────────────────

def _whole(raw, field: str, label: str, *, low: int = 0, high: int = 100_000_000) -> int | None:
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool) or not (low <= raw <= high):
        raise ProposalError(400, f"{label} isn't a number we can use")
    return raw


def normalize(booking: Booking, changes: dict, db: Session) -> dict:
    """The client's changes, checked the way a vendor's edit is, laid over
    the current terms: the whole proposed contract. Refuses a proposal that
    changes nothing."""
    if not isinstance(changes, dict):
        raise ProposalError(400, "Send the changes as an object")
    unknown = set(changes) - set(doc.TERMS_FIELDS)
    if unknown:
        raise ProposalError(400, f"Can't propose a change to {', '.join(sorted(unknown))}")
    current = doc.terms(booking)
    out = dict(current)

    when = ("date_iso", "date_end", "time_start", "time_end")
    for f in when:
        if f in changes:
            out[f] = changes[f] or None if f == "date_end" else changes[f]
    if any(out[f] != current[f] for f in when):
        if not isinstance(out["date_iso"], str):
            raise ProposalError(400, "The event needs a date")
        try:
            _check_dates(out["date_iso"], out["date_end"])
        except ContractError as e:
            raise ProposalError(e.status_code, e.detail)
        for f in ("time_start", "time_end"):
            if out[f] != current[f] and not (isinstance(out[f], str) and _TIME.match(out[f])):
                raise ProposalError(400, "Times are HH:MM, like 19:00")

    if "location" in changes:
        out["location"] = _text(changes["location"], 500) or "TBD"
    if "guest_count" in changes:
        out["guest_count"] = _whole(changes["guest_count"], "guest_count", "The guest count", low=1, high=100_000)

    if "line_items" in changes or "discount_cents" in changes:
        raw_items = changes.get("line_items", current["line_items"])
        if not raw_items:
            raise ProposalError(400, "Keep at least one package")
        items, _ = doc.normalize_line_items(raw_items, vendor_id=booking.vendor_id, db=db)
        discount = _whole(changes.get("discount_cents", current["discount_cents"]), "discount_cents", "The discount")
        _, total = doc.totals(items, discount)
        out.update(line_items=items, discount_cents=discount or None, amount_cents=total)

    if "payment_schedule" in changes:
        out["payment_schedule"] = doc.bare_schedule(
            doc.normalize_schedule(changes["payment_schedule"] or [], total_cents=out["amount_cents"])
        )
    elif current["payment_schedule"] and out["amount_cents"] != current["amount_cents"]:
        raise ProposalError(400, "The total changed — update the payment schedule to match it")

    if "terms_clauses" in changes:
        out["terms_clauses"] = doc.normalize_clauses(changes["terms_clauses"])
    if "cancellation_window_hours" in changes:
        out["cancellation_window_hours"] = _whole(
            changes["cancellation_window_hours"], "cancellation_window_hours", "The cancellation window", high=8760,
        )
    if "overtime_rate_cents" in changes:
        out["overtime_rate_cents"] = _whole(changes["overtime_rate_cents"], "overtime_rate_cents", "The overtime rate")

    if not changed_fields(current, out):
        raise ProposalError(400, "Change something before sending your proposal")
    return out


# ── The client, by their link ────────────────────────────────────────

def _history(booking: Booking, db: Session) -> dict:
    """Everything either side needs to compare versions: the proposals,
    newest first, and the versions they were made against."""
    proposals = (
        db.query(ContractProposal)
        .filter(ContractProposal.booking_id == booking.booking_id)
        .order_by(ContractProposal.created_at.desc())
        .limit(MAX_LISTED)
        .all()
    )
    revisions = (
        db.query(ContractRevision)
        .filter(ContractRevision.booking_id == booking.booking_id)
        .order_by(ContractRevision.revision.desc())
        .limit(MAX_LISTED)
        .all()
    )
    open_ = next((p for p in proposals if p.status == OPEN), None)
    return {
        "current_revision": booking.revision,
        "open_proposal": _dict(open_) if open_ else None,
        "proposals": [_dict(p) for p in proposals],
        "revisions": [
            {"revision": r.revision, "created_at": utc_iso(r.created_at), "terms": r.terms} for r in revisions
        ],
    }


def guest_history(*, contract_token: str, db: Session) -> dict:
    from app.services.guest_booking_service import _by_token

    return _history(_by_token(contract_token, db), db)


def propose(*, contract_token: str, base_revision: int, changes: dict, message: str | None, db: Session) -> dict:
    """The client suggests a version of their own. Same checks as a
    vendor's edit (the payments add up, every clause has a title and text),
    and only on a live, unsigned offer they're reading the latest version
    of. Replaces their open proposal, if they had one."""
    from app.services.guest_booking_service import (
        GuestBookingError,
        _by_token,
        _client_label,
        _pretty_date,
        _require_live,
        _require_open_offer,
        _tell_vendor,
    )

    booking = _by_token(contract_token, db)
    _require_live(booking)
    if booking.signed_at is not None:
        raise GuestBookingError(400, "This contract is signed, so it can't be changed now")
    _require_open_offer(booking)
    if not booking.guest_email:
        raise GuestBookingError(400, "Add your email first so we can tell you when they reply")
    if booking.revision is None:
        booking.revision = 1  # a contract from before revisions were counted
    if base_revision != booking.revision:
        raise GuestBookingError(
            409, "Your vendor updated this contract after you opened it — review the latest version first",
        )

    proposed = normalize(booking, changes, db)
    fields = changed_fields(doc.terms(booking), proposed)
    doc.snapshot_revision(db, booking)
    replaced = close_open(db, booking, "superseded", "client")
    p = ContractProposal(
        booking_id=booking.booking_id,
        base_revision=booking.revision,
        proposed=proposed,
        message=_text(message, MAX_MESSAGE),
        status=OPEN,
        created_at=_now(),
    )
    db.add(p)
    db.flush()
    doc.record(db, booking, "proposal_sent", "client", {
        "proposal_id": p.proposal_id, "fields": fields,
        "replaced": replaced.proposal_id if replaced else None,
    })
    db.commit()

    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    n = len(fields)
    _tell_vendor(
        booking,
        f"{_client_label(booking)} proposed changes",
        f"{n} change{'s' if n != 1 else ''} to {service.name if service else 'your contract'} on "
        f"{_pretty_date(booking.date_iso)}. Review them on the contract's page.",
        "contract_changes_proposed", db,
    )
    return _history(booking, db)


def withdraw(*, contract_token: str, proposal_id: str, db: Session) -> dict:
    from app.services.guest_booking_service import GuestBookingError, _by_token

    booking = _by_token(contract_token, db)
    p = db.query(ContractProposal).filter(
        ContractProposal.proposal_id == proposal_id, ContractProposal.booking_id == booking.booking_id,
    ).first()
    if p is None:
        raise GuestBookingError(404, "Proposal not found")
    if p.status != OPEN:
        raise GuestBookingError(400, "Your vendor has already answered this proposal")
    p.status = "withdrawn"
    p.responded_at = _now()
    doc.record(db, booking, "proposal_withdrawn", "client", {"proposal_id": p.proposal_id})
    db.commit()
    return _history(booking, db)


# ── The vendor ───────────────────────────────────────────────────────

def _own_booking(booking_id: str, caller_user_id: str, db: Session) -> tuple[Booking, Vendor]:
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking or not booking.contract_token:
        raise ProposalError(404, "Contract not found")
    return booking, _own_vendor(vendor_id=booking.vendor_id, caller_user_id=caller_user_id, db=db)


def vendor_history(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    booking, _ = _own_booking(booking_id, caller_user_id, db)
    return _history(booking, db)


_ANSWERED = {
    "accepted": "You've already accepted this proposal",
    "declined": "You've already declined this proposal",
    "revised": "You've already sent a new version for this proposal",
    "superseded": "Your client has sent a newer proposal, or the contract changed since",
    "withdrawn": "Your client withdrew this proposal",
}


def open_for_vendor(booking: Booking, proposal_id: str, db: Session) -> ContractProposal:
    p = db.query(ContractProposal).filter(
        ContractProposal.proposal_id == proposal_id, ContractProposal.booking_id == booking.booking_id,
    ).first()
    if p is None:
        raise ProposalError(404, "Proposal not found")
    if p.status != OPEN:
        raise ProposalError(409, _ANSWERED.get(p.status, "This proposal is closed"))
    if p.base_revision != booking.revision:
        raise ProposalError(409, "The contract changed since this proposal was made")
    return p


def _answer(booking: Booking, p: ContractProposal, status: str, note: str | None, db: Session) -> None:
    p.status = status
    p.responded_at = _now()
    p.response_note = _text(note, MAX_NOTE)
    if status in ("accepted", "revised"):
        p.result_revision = booking.revision
    doc.record(db, booking, f"proposal_{status}", "vendor", {
        "proposal_id": p.proposal_id, "revision": booking.revision, "note": p.response_note,
    })


def accept(*, booking_id: str, proposal_id: str, caller_user_id: str, db: Session, note: str | None = None) -> dict:
    """The client's terms become the next revision, through the same edit
    path as the vendor's own — a changed date gets the overlap check — and
    go back to them to sign, with the hold restarted."""
    booking, vendor = _own_booking(booking_id, caller_user_id, db)
    p = open_for_vendor(booking, proposal_id, db)
    update = {f: p.proposed.get(f) for f in changed_fields(doc.terms(booking), p.proposed) if f != "amount_cents"}
    apply_update(booking, vendor, update, db)
    _answer(booking, p, "accepted", note, db)
    resend(booking, vendor, db)
    db.commit()
    email_reply(booking, vendor, p, db)
    return get_contract(booking_id=booking_id, caller_user_id=caller_user_id, db=db)


def decline(*, booking_id: str, proposal_id: str, caller_user_id: str, db: Session, note: str | None = None) -> dict:
    """The current version stands. The hold carries on as it was."""
    booking, vendor = _own_booking(booking_id, caller_user_id, db)
    p = open_for_vendor(booking, proposal_id, db)
    _answer(booking, p, "declined", note, db)
    db.commit()
    email_reply(booking, vendor, p, db)
    return get_contract(booking_id=booking_id, caller_user_id=caller_user_id, db=db)


def mark_revised(booking: Booking, vendor: Vendor, p: ContractProposal, db: Session) -> None:
    """Revise: the vendor's edit (already applied) answers the proposal and
    goes to the client as the next version."""
    _answer(booking, p, "revised", None, db)
    resend(booking, vendor, db)


def email_reply(booking: Booking, vendor: Vendor, p: ContractProposal, db: Session) -> None:
    """Tell the client how their proposal was answered. Best effort, after
    the answer has committed."""
    if not booking.guest_email:
        return
    who = _vendor_name(db.query(User).filter(User.user_id == vendor.user_id).first()) or "Your vendor"
    link = f'<p><a href="{_link(booking)}">Open the contract</a> — no account needed.</p>'
    note = f"<p>They said: “{p.response_note}”</p>" if p.response_note else ""
    if p.status == "accepted":
        subject = f"{who} accepted your changes"
        lead = "<p>Your changes are in the contract now. Read it over and sign when you're ready.</p>"
    elif p.status == "revised":
        subject = f"{who} sent a new version of your contract"
        lead = "<p>They've answered your changes with a new version. See what changed, then sign or reply.</p>"
    else:
        subject = f"{who} kept your contract as it was"
        lead = "<p>They'd rather keep the current version. You can still sign it, or propose something else.</p>"
    send_email(to=booking.guest_email, subject=subject, html=lead + note + link)
