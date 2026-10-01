"""What a contract says — line items, payment schedule, clauses — and the
record around it: the signed snapshot and the timeline. docs/DECISIONS.md #16.

Everything a vendor puts in a contract is snapshotted onto the booking. A
package or add-on is looked up once, to check it's the vendor's own and to
fill in a name or price the vendor left blank; after that nothing joins back
to it, so editing or archiving a package never rewrites a contract.

The legacy single-deposit fields (Booking.deposit_percent/_amount_cents,
deposit_marked_paid_at/confirmed_received_at, payment_status and the
manual_payment_* pair) are kept in step with the schedule by
sync_legacy_payment_fields, so every reader of those — the bookings page,
earnings, the pipeline, iOS — sees a scheduled contract the way it saw one
with a deposit.

Pure bookkeeping: callers own the session and commit.
"""
import hashlib
import json
import secrets
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.db.models import Booking, ContractEvent, Service
from app.models.schemas import PaymentStatus
from app.utils.timeutil import utc_iso

ITEM_KINDS = ("package", "addon", "custom")
UNITS = ("event", "person", "hour", "day", "item")
DUE_TYPES = ("on_signing", "date", "before_event")
MAX_ITEMS = 30
MAX_INSTALLMENTS = 12
MAX_CLAUSES = 30


class DocumentError(Exception):
    """A contract document the vendor sent doesn't hold together. Always a
    400 — it's about what was typed, not about who's asking."""

    def __init__(self, detail: str):
        self.status_code = 400
        self.detail = detail
        super().__init__(detail)


def _id() -> str:
    return secrets.token_hex(6)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _text(raw, limit: int) -> str:
    return (raw or "").strip()[:limit] if isinstance(raw, str) or raw is None else str(raw)[:limit]


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


# ── Line items ───────────────────────────────────────────────────────

def normalize_line_items(raw: list, *, vendor_id: str, db: Session) -> tuple[list[dict], str]:
    """Validate and total a vendor's line items. Returns (items, the first
    package's service_id — Booking.service_id can't be null, so it names the
    lead package).

    Prices come from the vendor, not the catalogue: quoting a package below
    list for one client is exactly what a contract is for. The catalogue
    only fills a price or name left blank.
    """
    if not raw:
        raise DocumentError("Add at least one package")
    if len(raw) > MAX_ITEMS:
        raise DocumentError(f"A contract can have at most {MAX_ITEMS} line items")

    services = {s.service_id: s for s in db.query(Service).filter(Service.vendor_id == vendor_id).all()}
    items, primary = [], None
    for r in raw:
        kind = r.get("kind")
        if kind not in ITEM_KINDS:
            raise DocumentError("Each line item is a package, an add-on, or a custom item")
        service = None
        catalogue_name, catalogue_cents, catalogue_unit = None, None, None

        if kind in ("package", "addon"):
            service = services.get(r.get("service_id"))
            if service is None:
                raise DocumentError("A line item names a package that isn't one of yours")
            if service.status == "archived":
                raise DocumentError(f"“{service.name}” is archived — restore it before using it in a contract")
        if kind == "package":
            catalogue_name, catalogue_unit = service.name, service.price_unit
            catalogue_cents = round((service.price or 0) * 100)
            primary = primary or service.service_id
        elif kind == "addon":
            addon = next((a for a in (service.add_ons or []) if a.get("id") == r.get("addon_id")), None)
            if addon is None:
                raise DocumentError(f"That add-on isn't on “{service.name}” any more")
            catalogue_name, catalogue_unit = addon.get("name"), addon.get("price_unit")
            catalogue_cents = round((addon.get("price") or 0) * 100)

        name = _text(r.get("name"), 200) or catalogue_name
        if not name:
            raise DocumentError("Every custom line item needs a name")
        unit = r.get("unit") or catalogue_unit or "event"
        if unit not in UNITS:
            unit = "event"
        unit_price = r.get("unit_price_cents")
        if unit_price is None:
            unit_price = catalogue_cents
        if unit_price is None or not isinstance(unit_price, int) or unit_price < 0:
            raise DocumentError(f"“{name}” needs a price")
        quantity = r.get("quantity", 1)
        if not isinstance(quantity, (int, float)) or isinstance(quantity, bool) or not (0 < quantity <= 100_000):
            raise DocumentError(f"“{name}” needs a quantity above zero")

        items.append({
            "id": _text(r.get("id"), 40) or _id(),
            "kind": kind,
            "service_id": service.service_id if service else None,
            "addon_id": r.get("addon_id") if kind == "addon" else None,
            "name": name,
            "description": _text(r.get("description"), 1000) or None,
            "unit": unit,
            "unit_price_cents": unit_price,
            "quantity": quantity,
            "total_cents": round(unit_price * quantity),
        })

    if primary is None:
        raise DocumentError("A contract needs at least one of your packages")
    return items, primary


def totals(items: list[dict], discount_cents: int | None) -> tuple[int, int]:
    """(subtotal, total). A discount can't take the total to zero or below."""
    subtotal = sum(i["total_cents"] for i in items)
    discount = discount_cents or 0
    if discount < 0:
        raise DocumentError("A discount can't be negative")
    total = subtotal - discount
    if total <= 0:
        raise DocumentError("The total after the discount must be more than $0")
    return subtotal, total


def single_item(service: Service, amount_cents: int) -> list[dict]:
    """The one-line version of a contract made the old way — one package, one
    price — so every contract reads the same on both sides."""
    return [{
        "id": _id(), "kind": "package", "service_id": service.service_id, "addon_id": None,
        "name": service.name, "description": None, "unit": "event",
        "unit_price_cents": amount_cents, "quantity": 1, "total_cents": amount_cents,
    }]


# ── Payment schedule ─────────────────────────────────────────────────

def normalize_schedule(raw: list, *, total_cents: int) -> list[dict]:
    """Installments that add up to the total, to the cent. Payment marks are
    never taken from the request — a schedule can only change before signing,
    and nothing is paid before signing."""
    if not raw:
        raise DocumentError("A payment schedule needs at least one payment")
    if len(raw) > MAX_INSTALLMENTS:
        raise DocumentError(f"A payment schedule can have at most {MAX_INSTALLMENTS} payments")
    out = []
    for r in raw:
        label = _text(r.get("label"), 80)
        if not label:
            raise DocumentError("Every payment needs a name, like “Deposit” or “Final balance”")
        amount = r.get("amount_cents")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise DocumentError(f"“{label}” needs an amount above $0")
        due_type = r.get("due_type") or "on_signing"
        if due_type not in DUE_TYPES:
            raise DocumentError(f"“{label}” has an unknown due date rule")
        due_date, due_days = None, None
        if due_type == "date":
            try:
                due_date = date.fromisoformat(r.get("due_date") or "").isoformat()
            except ValueError:
                raise DocumentError(f"“{label}” needs a due date")
        elif due_type == "before_event":
            due_days = r.get("due_days")
            if not isinstance(due_days, int) or not (0 <= due_days <= 730):
                raise DocumentError(f"“{label}” needs how many days before the event it's due")
        out.append({
            "id": _text(r.get("id"), 40) or _id(),
            "label": label,
            "amount_cents": amount,
            "due_type": due_type,
            "due_date": due_date,
            "due_days": due_days,
            "marked_paid_at": None,
            "confirmed_at": None,
        })
    scheduled = sum(i["amount_cents"] for i in out)
    if scheduled != total_cents:
        raise DocumentError(
            f"The payments add up to {_money(scheduled)} but the total is {_money(total_cents)}"
        )
    return out


def due_on(installment: dict, booking: Booking) -> str | None:
    """The calendar date a payment is due, for display. Null for "on signing"
    until it's signed."""
    kind = installment.get("due_type")
    if kind == "date":
        return installment.get("due_date")
    if kind == "before_event":
        try:
            return (date.fromisoformat(booking.date_iso) - timedelta(days=installment.get("due_days") or 0)).isoformat()
        except (TypeError, ValueError):
            return None
    return booking.signed_at.date().isoformat() if booking.signed_at else None


def effective_due(installment: dict, booking: Booking) -> date | None:
    """When a payment is actually due: its schedule date, but never before
    the day the contract was signed. A contract signed ten days before the
    event has a "14 days before" balance whose date is already gone; the
    first thing that should happen is a reminder, not an overdue notice."""
    raw = due_on(installment, booking)
    if raw is None or booking.signed_at is None:
        return None
    due = date.fromisoformat(raw)
    return max(due, booking.signed_at.date())


def schedule_view(booking: Booking) -> list[dict] | None:
    if not booking.payment_schedule:
        return None
    return [
        {
            **i,
            "due_on": due_on(i, booking),
            # What reminders count from, and what a client should read as the
            # due date: never before the signing day. Null until signed.
            "effective_due": _iso_date(effective_due(i, booking)),
            "marked_paid_at": utc_iso(i.get("marked_paid_at")),
            "confirmed_at": utc_iso(i.get("confirmed_at")),
        }
        for i in booking.payment_schedule
    ]


def _iso_date(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def sync_legacy_payment_fields(booking: Booking) -> None:
    """Mirror the schedule into the single-deposit fields every older reader
    uses: with two or more payments, the first is "the deposit"; the whole
    thing is marked paid once every remaining payment is, and confirmed once
    every payment is."""
    schedule = booking.payment_schedule
    if not schedule:
        return
    if len(schedule) >= 2:
        first = schedule[0]
        booking.deposit_amount_cents = first["amount_cents"]
        booking.deposit_percent = round(100 * first["amount_cents"] / booking.amount_cents)
        booking.deposit_marked_paid_at = _parse(first.get("marked_paid_at"))
        booking.deposit_confirmed_received_at = _parse(first.get("confirmed_at"))
        rest = schedule[1:]
    else:
        booking.deposit_amount_cents = None
        booking.deposit_percent = None
        booking.deposit_marked_paid_at = None
        booking.deposit_confirmed_received_at = None
        rest = schedule

    if all(i.get("confirmed_at") for i in schedule):
        booking.payment_status = PaymentStatus.CONFIRMED_PAID.value
        booking.manual_payment_marked_at = max(_parse(i["marked_paid_at"]) for i in rest)
        booking.manual_payment_confirmed_at = max(_parse(i["confirmed_at"]) for i in schedule)
    elif all(i.get("marked_paid_at") for i in rest):
        booking.payment_status = PaymentStatus.MARKED_PAID.value
        booking.manual_payment_marked_at = max(_parse(i["marked_paid_at"]) for i in rest)
        booking.manual_payment_confirmed_at = None
    else:
        booking.payment_status = PaymentStatus.UNPAID.value
        booking.manual_payment_marked_at = None
        booking.manual_payment_confirmed_at = None


def _update_schedule(booking: Booking, ids: set[str], *, mark: bool, confirm: bool) -> list[dict]:
    """Set marks on the given installments. A new list is assigned, not the
    old one mutated — the JSON column doesn't notice in-place changes."""
    now = _now().isoformat()
    changed, schedule = [], []
    for i in booking.payment_schedule:
        i = dict(i)
        if i["id"] in ids:
            if mark and not i.get("marked_paid_at"):
                i["marked_paid_at"] = now
                changed.append(i)
            if confirm and not i.get("confirmed_at"):
                # A vendor can confirm money the client never marked as sent
                # — they're the one who'd know it arrived.
                i["marked_paid_at"] = i.get("marked_paid_at") or now
                i["confirmed_at"] = now
                changed.append(i)
        schedule.append(i)
    booking.payment_schedule = schedule
    sync_legacy_payment_fields(booking)
    return changed


def installment(booking: Booking, installment_id: str) -> dict:
    found = next((i for i in booking.payment_schedule or [] if i["id"] == installment_id), None)
    if found is None:
        raise DocumentError("That payment isn't on this contract")
    return found


def mark_paid(booking: Booking, installment_ids: list[str]) -> list[dict]:
    """The client says these payments are sent. Returns the ones that
    changed — empty if they were all marked already."""
    return _update_schedule(booking, set(installment_ids), mark=True, confirm=False)


def confirm_received(booking: Booking, installment_ids: list[str]) -> list[dict]:
    return _update_schedule(booking, set(installment_ids), mark=False, confirm=True)


# ── Terms ────────────────────────────────────────────────────────────

def normalize_clauses(raw: list | None) -> list[dict] | None:
    """Clause text exactly as it will be sent. The key is the vendor's own
    handle for a clause across contracts (cancellation, travel, …); the text
    is what's agreed, so it's what the signed snapshot keeps."""
    if not raw:
        return None
    if len(raw) > MAX_CLAUSES:
        raise DocumentError(f"A contract can have at most {MAX_CLAUSES} clauses")
    out = []
    for r in raw:
        title = _text(r.get("title"), 120)
        body = _text(r.get("body"), 5000)
        if not title or not body:
            raise DocumentError("Every clause needs a title and some text")
        out.append({"key": _text(r.get("key"), 60) or _id(), "title": title, "body": body})
    return out


# ── The editor's layout (0067) ───────────────────────────────────────

# The structured blocks the rest of the app reads; each appears at most once.
STRUCTURED_BLOCKS = ("parties", "event", "items", "schedule", "signature")
MAX_LAYOUT_BLOCKS = 40


def normalize_layout(raw: list | None) -> tuple[list[dict] | None, list[dict] | None]:
    """The document editor's blocks, in order: terms sections (title + text)
    and where the structured blocks sit among them. Returns (layout,
    clauses) — the terms sections become terms_clauses too, in the same
    order, so the signing page (which reads clauses) shows what the editor
    shows. The structured blocks carry no content of their own here: the
    event, items and schedule live in their own columns."""
    if raw is None:
        return None, None
    if len(raw) > MAX_LAYOUT_BLOCKS:
        raise DocumentError(f"A contract can have at most {MAX_LAYOUT_BLOCKS} blocks")
    layout: list[dict] = []
    seen: set[str] = set()
    terms: list[dict] = []
    for r in raw:
        kind = _text(r.get("type"), 20)
        block_id = _text(r.get("id"), 60) or _id()
        if kind == "terms":
            terms.append({"key": block_id, "title": r.get("title"), "body": r.get("body")})
            layout.append({"id": block_id, "type": "terms"})
        elif kind in STRUCTURED_BLOCKS:
            if kind in seen:
                raise DocumentError(f"A contract can have only one {kind} block")
            seen.add(kind)
            layout.append({"id": block_id, "type": kind})
        else:
            raise DocumentError(f"Unknown contract block: {kind or 'missing type'}")
    clauses = normalize_clauses(terms)
    return layout, clauses


# ── The signed record ────────────────────────────────────────────────

def agreement(booking: Booking) -> dict:
    """Everything the client is agreeing to, in one plain structure — what a
    signature freezes and hashes."""
    return {
        "revision": booking.revision,
        "date_iso": booking.date_iso,
        "date_end": booking.date_end,
        "time_start": booking.time_start,
        "time_end": booking.time_end,
        "location": booking.location,
        "guest_count": booking.guest_count,
        "client": {
            "name": booking.guest_name,
            "email": booking.guest_email,
            "phone": booking.guest_phone,
        },
        "line_items": booking.line_items,
        "discount_cents": booking.discount_cents,
        "amount_cents": booking.amount_cents,
        "payment_schedule": [
            {k: i[k] for k in ("id", "label", "amount_cents", "due_type", "due_date", "due_days")}
            for i in booking.payment_schedule or []
        ] or None,
        "deposit_percent": booking.deposit_percent,
        "deposit_amount_cents": booking.deposit_amount_cents,
        "cancellation_window_hours": booking.cancellation_window_hours,
        "overtime_rate_cents": booking.overtime_rate_cents,
        "addon_rate_cents": booking.addon_rate_cents,
        "terms_clauses": booking.terms_clauses,
        "contract_terms": booking.contract_terms,
        "document_title": booking.document_title,
        "document_layout": booking.document_layout,
    }


def freeze(booking: Booking) -> None:
    """At signing: keep exactly what was signed, and its fingerprint, so a
    later dispute can be settled against the text rather than memory."""
    snapshot = {
        **agreement(booking),
        "signer_name": booking.signer_name,
        "signed_at": booking.signed_at.isoformat(),
    }
    booking.signed_snapshot = snapshot
    booking.signed_snapshot_sha256 = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


# ── Timeline ─────────────────────────────────────────────────────────

def record(db: Session, booking: Booking, kind: str, actor: str, detail: dict | None = None) -> None:
    db.add(ContractEvent(booking_id=booking.booking_id, at=_now(), kind=kind, actor=actor, detail=detail))


def _derived_events(booking: Booking) -> list[dict]:
    """A timeline for a contract written before events were recorded,
    rebuilt from the timestamps the booking already has."""
    stamps = [
        (booking.confirmed_at, "created", "vendor"),
        (booking.sent_at, "sent", "vendor"),
        (booking.viewed_at, "viewed", "client"),
        (booking.signed_at, "signed", "client"),
        (booking.deposit_marked_paid_at, "payment_marked", "client"),
        (booking.deposit_confirmed_received_at, "payment_confirmed", "vendor"),
        (booking.manual_payment_marked_at, "payment_marked", "client"),
        (booking.manual_payment_confirmed_at, "payment_confirmed", "vendor"),
        (booking.declined_at, "declined", "client"),
        (booking.voided_at, "voided", "vendor"),
    ]
    return [
        {"at": at.replace(tzinfo=None) if at.tzinfo else at, "kind": kind, "actor": actor, "detail": None}
        for at, kind, actor in stamps if at
    ]


def timeline(booking: Booking, db: Session, state: str | None) -> list[dict]:
    rows = (
        db.query(ContractEvent)
        .filter(ContractEvent.booking_id == booking.booking_id)
        .order_by(ContractEvent.at)
        .all()
    )
    events = (
        [{"at": r.at, "kind": r.kind, "actor": r.actor, "detail": r.detail} for r in rows]
        if rows else _derived_events(booking)
    )
    # Expiry is never stored (contract_service.contract_state), so neither
    # is its event.
    if state == "expired" and booking.hold_expires_at:
        events.append({"at": booking.hold_expires_at, "kind": "expired", "actor": "system", "detail": None})
    events.sort(key=lambda e: e["at"])
    return [{**e, "at": utc_iso(e["at"])} for e in events]
