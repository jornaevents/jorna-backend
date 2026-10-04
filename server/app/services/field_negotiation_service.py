"""Field-by-field contract negotiation — docs/DECISIONS.md #26.

Replaces the whole-proposal flow (#23) for contracts whose
Booking.negotiation_mode is "fields". Each negotiable value of the contract
is a *field* with a stable key (the same ids the web app's lib/negotiation
uses):

    event.date  event.time  event.location  event.guests
    line:<id>.quantity  line:<id>.price  line:<id>.included  line:new:<tag>
    discount  policy.cancellation  policy.overtime
    clause:<key>.included  schedule

A field is Agreed (no row), waiting on one side, or Settled. Sides take
strict turns; a send answers every field waiting on the sender (accept,
counter, keep) and may propose changes to Agreed ones. Accepting settles a
field and writes its value into the contract as a new revision, so the
contract only ever holds agreed values and open asks sit beside it.

Rules the server owns, so neither page can get them wrong:
- whose turn it is, and that the sender read the latest round;
- what each side may change: the client never the payment schedule, nothing
  in a group the vendor locked, and only the vendor's own packages;
- that nothing waiting on the sender is left unanswered;
- that a settled field stays settled unless the vendor reopens it;
- that the payment schedule follows the total when a price settles.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import (
    Booking,
    ContractDraft,
    ContractRevision,
    NegotiationField,
    NegotiationSend,
    Service,
    User,
    Vendor,
)
from app.services import contract_document as doc
from app.services.contract_service import (
    ContractError,
    _check_dates,
    _link,
    _own_vendor,
    _vendor_name,
    apply_update,
    resend,
)
from app.services.email_service import send_email
from app.utils.timeutil import utc_iso

MODE = "fields"
SIDES = ("client", "vendor")
LOCK_GROUPS = ("prices", "event", "policies", "clauses")
ACTIONS = ("accept", "counter", "keep", "change", "reopen")
WAITING = {"client": "waiting_client", "vendor": "waiting_vendor"}
SETTLED = "settled"
NUDGE_ROUND = 6
MAX_ANSWERS = 60
MAX_MESSAGE = 2000
MAX_NOTE = 1000
MAX_DRAFT_BYTES = 100_000
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class FieldNegotiationError(ContractError):
    """A send or a read the negotiation rules refuse. A ContractError, so the
    vendor's routes report it like any other contract error."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _other(side: str) -> str:
    return "vendor" if side == "client" else "client"


def _text(raw, limit: int) -> str | None:
    return raw.strip()[:limit] or None if isinstance(raw, str) else None


# ── Opting in ────────────────────────────────────────────────────────

def start(booking: Booking, vendor: Vendor) -> None:
    """Make a new contract negotiate field by field, if that's switched on.
    Called where a contract is created; existing contracts keep the flow
    they started with."""
    from app.config import FIELD_NEGOTIATION

    if FIELD_NEGOTIATION and booking.negotiation_mode is None:
        booking.negotiation_mode = MODE
        booking.negotiation_round = 1
        booking.negotiation_turn = "client"


def copy_locks(booking: Booking, vendor: Vendor) -> None:
    """The vendor's not-negotiable groups, as of the contract's first send —
    a later change in their Defaults doesn't move a live negotiation."""
    if booking.negotiation_mode == MODE and booking.negotiation_locks is None:
        booking.negotiation_locks = [g for g in (vendor.negotiation_locks or []) if g in LOCK_GROUPS]


def is_fields(booking: Booking) -> bool:
    return booking.negotiation_mode == MODE


# ── Fields: what a version says, one value per key ───────────────────

def group_of(key: str) -> str:
    if key.startswith("event."):
        return "event"
    if key == "discount" or (key.startswith("line:") and key.endswith(".price")):
        return "prices"
    if key.startswith("line:"):
        return "items"
    if key.startswith("policy."):
        return "policies"
    if key.startswith("clause:"):
        return "clauses"
    if key == "schedule":
        return "schedule"
    raise FieldNegotiationError(400, f"There's no field called {key}")


def fields_of(terms: dict) -> dict:
    """A version's terms as negotiable values, keyed."""
    out = {
        "event.date": {"date_iso": terms.get("date_iso"), "date_end": terms.get("date_end")},
        "event.time": {"time_start": terms.get("time_start"), "time_end": terms.get("time_end")},
        "event.location": terms.get("location"),
        "event.guests": terms.get("guest_count"),
    }
    for line in terms.get("line_items") or []:
        out[f"line:{line['id']}.quantity"] = line["quantity"]
        out[f"line:{line['id']}.price"] = line["unit_price_cents"]
        out[f"line:{line['id']}.included"] = True
    out["discount"] = terms.get("discount_cents") or 0
    out["policy.cancellation"] = terms.get("cancellation_window_hours")
    out["policy.overtime"] = terms.get("overtime_rate_cents")
    for clause in terms.get("terms_clauses") or []:
        out[f"clause:{clause['key']}.included"] = True
    out["schedule"] = terms.get("payment_schedule")
    return out


def _line_id(key: str) -> str:
    return key[len("line:"):].rsplit(".", 1)[0]


def _clause_key(key: str) -> str:
    return key[len("clause:"):].rsplit(".", 1)[0]


def label(key: str, terms: dict, value=None) -> str:
    """How the side panel names a field: plain words, the item's own name."""
    names = {
        "event.date": "Event date", "event.time": "Start and end time", "event.location": "Venue",
        "event.guests": "Guest count", "discount": "Discount", "policy.cancellation": "Cancellation window",
        "policy.overtime": "Overtime rate", "schedule": "Payment schedule",
    }
    if key in names:
        return names[key]
    if key.startswith("line:new:"):
        return f"Add {(value or {}).get('name') or 'an item'}"
    if key.startswith("line:"):
        line = next((li for li in terms.get("line_items") or [] if li["id"] == _line_id(key)), None)
        name = line["name"] if line else "An item"
        part = key.rsplit(".", 1)[1]
        return {"quantity": f"{name}: quantity", "price": f"{name}: price", "included": f"Include {name}"}[part]
    if key.startswith("clause:"):
        clause = next((c for c in terms.get("terms_clauses") or [] if c["key"] == _clause_key(key)), None)
        return f"Include “{clause['title'] if clause else 'a section'}”"
    return key


def _whole(raw, what: str, *, low: int = 0, high: int = 100_000_000, nullable: bool = False):
    if raw is None and nullable:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool) or not (low <= raw <= high):
        raise FieldNegotiationError(400, f"{what} isn't a number we can use")
    return raw


def validate(key: str, value, terms: dict, vendor_id: str, db: Session):
    """A proposed value, checked and in its stored shape. Item and schedule
    values get their full check again when they settle, against the terms
    as they are then."""
    current = fields_of(terms)
    if key == "event.date":
        if not isinstance(value, dict) or not isinstance(value.get("date_iso"), str):
            raise FieldNegotiationError(400, "The event needs a date")
        end = value.get("date_end") or None
        try:
            _check_dates(value["date_iso"], end)
        except ContractError as e:
            raise FieldNegotiationError(e.status_code, e.detail)
        return {"date_iso": value["date_iso"], "date_end": end}
    if key == "event.time":
        if not isinstance(value, dict) or not all(
            isinstance(value.get(f), str) and _TIME.match(value[f]) for f in ("time_start", "time_end")
        ):
            raise FieldNegotiationError(400, "Times are HH:MM, like 19:00")
        return {"time_start": value["time_start"], "time_end": value["time_end"]}
    if key == "event.location":
        text = _text(value, 500)
        if not text:
            raise FieldNegotiationError(400, "Say where the event is")
        return text
    if key == "event.guests":
        return _whole(value, "The guest count", low=1, high=100_000, nullable=True)
    if key == "discount":
        return _whole(value, "The discount")
    if key == "policy.cancellation":
        return _whole(value, "The cancellation window", high=8760, nullable=True)
    if key == "policy.overtime":
        return _whole(value, "The overtime rate", nullable=True)
    if key == "schedule":
        if not isinstance(value, list):
            raise FieldNegotiationError(400, "Send the payment schedule as a list")
        total = sum(i.get("amount_cents") or 0 for i in value if isinstance(i, dict))
        return doc.bare_schedule(doc.normalize_schedule(value, total_cents=total))
    if key.startswith("line:new:"):
        if not isinstance(value, dict) or not value.get("service_id"):
            raise FieldNegotiationError(400, "A new item has to be one of the vendor's packages or add-ons")
        raw = {
            "kind": "addon" if value.get("addon_id") else "package",
            "service_id": value["service_id"],
            "addon_id": value.get("addon_id"),
            "quantity": value.get("quantity", 1),
        }
        if value.get("unit_price_cents") is not None:
            raw["unit_price_cents"] = value["unit_price_cents"]
        if raw["kind"] == "package":
            (item,), _ = doc.normalize_line_items([raw], vendor_id=vendor_id, db=db)
        else:
            (item,), _ = _addon_only(raw, vendor_id, db)
        return {k: item[k] for k in ("kind", "service_id", "addon_id", "name", "unit", "unit_price_cents", "quantity")}
    if key.startswith(("line:", "clause:")):
        base = key.rsplit(".", 1)[0]
        if not any(k.startswith(base + ".") for k in current):
            raise FieldNegotiationError(400, "That item or section isn't in the contract")
        part = key.rsplit(".", 1)[1]
        if part == "included":
            if not isinstance(value, bool):
                raise FieldNegotiationError(400, "Include is yes or no")
            return value
        if part == "quantity":
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not (0 < value <= 100_000):
                raise FieldNegotiationError(400, "A quantity has to be above zero")
            return value
        if part == "price":
            return _whole(value, "The price")
    raise FieldNegotiationError(400, f"There's no field called {key}")


def _addon_only(raw: dict, vendor_id: str, db: Session):
    """normalize_line_items insists on a package somewhere in the list; an
    add-on asked for on its own is checked beside a stand-in package."""
    service = db.query(Service).filter(Service.service_id == raw["service_id"], Service.vendor_id == vendor_id).first()
    if service is None:
        raise FieldNegotiationError(400, "A line item names a package that isn't one of the vendor's")
    stand_in = {"kind": "package", "service_id": service.service_id, "quantity": 1}
    items, primary = doc.normalize_line_items([stand_in, raw], vendor_id=vendor_id, db=db)
    return [items[1]], primary


# ── Writing settled values into the contract ─────────────────────────

def follow_total(schedule: list[dict] | None, total: int) -> list[dict] | None:
    """The payment schedule rescaled to a new total, in proportion, with the
    remainder on the last payment — so a deposit stays roughly the share it
    was."""
    if not schedule:
        return schedule
    old = sum(i["amount_cents"] for i in schedule) or 1
    out = [dict(i) for i in schedule]
    running = 0
    for i in out[:-1]:
        i["amount_cents"] = max(1, round(i["amount_cents"] * total / old))
        running += i["amount_cents"]
    out[-1]["amount_cents"] = total - running
    if out[-1]["amount_cents"] <= 0:
        raise FieldNegotiationError(400, "The new total is too small for this payment schedule")
    return out


def _update_for(booking: Booking, settled: dict, db: Session) -> dict:
    """The apply_update payload that writes these settled values into the
    contract — whole line items, clauses and schedule, since those are
    replaced as lists."""
    terms = doc.terms(booking)
    update: dict = {}
    items = copy.deepcopy(terms["line_items"] or [])
    clauses = copy.deepcopy(terms["terms_clauses"] or [])
    items_changed = clauses_changed = False

    for key, value in settled.items():
        if key == "event.date":
            update.update(date_iso=value["date_iso"], date_end=value["date_end"])
        elif key == "event.time":
            update.update(time_start=value["time_start"], time_end=value["time_end"])
        elif key == "event.location":
            update["location"] = value
        elif key == "event.guests":
            update["guest_count"] = value
        elif key == "discount":
            update["discount_cents"] = value or None
        elif key == "policy.cancellation":
            update["cancellation_window_hours"] = value
        elif key == "policy.overtime":
            update["overtime_rate_cents"] = value
        elif key == "schedule":
            update["payment_schedule"] = value
        elif key.startswith("line:new:"):
            items.append(dict(value))
            items_changed = True
        elif key.startswith("line:"):
            lid, part = _line_id(key), key.rsplit(".", 1)[1]
            for li in items:
                if li["id"] == lid:
                    if part == "quantity":
                        li["quantity"] = value
                    elif part == "price":
                        li["unit_price_cents"] = value
                    elif part == "included" and value is False:
                        li["_drop"] = True
            items_changed = True
        elif key.startswith("clause:") and value is False:
            ck = _clause_key(key)
            clauses = [c for c in clauses if c["key"] != ck]
            clauses_changed = True

    if items_changed:
        update["line_items"] = [{k: v for k, v in li.items() if k != "total_cents"} for li in items if not li.get("_drop")]
    if clauses_changed:
        update["terms_clauses"] = clauses
    if ("line_items" in update or "discount_cents" in update) and "payment_schedule" not in update and terms["payment_schedule"]:
        normalized, _ = doc.normalize_line_items(
            update.get("line_items", terms["line_items"]), vendor_id=booking.vendor_id, db=db,
        )
        _, total = doc.totals(normalized, update.get("discount_cents", terms["discount_cents"]))
        if total != terms["amount_cents"]:
            update["payment_schedule"] = follow_total(terms["payment_schedule"], total)
    return update


def _settle(booking: Booking, vendor: Vendor, settled: dict, db: Session) -> None:
    """Write settled values into the contract as one new revision, through
    the same path as a vendor's edit: a moved date gets the overlap check,
    the totals and schedule must add up."""
    current = fields_of(doc.terms(booking))
    changed = {k: v for k, v in settled.items() if current.get(k) != v}
    if changed:
        apply_update(booking, vendor, _update_for(booking, changed, db), db)


# ── Reading ──────────────────────────────────────────────────────────

def packages_for(booking: Booking, side: str, db: Session) -> list[dict]:
    """What this side may add as a new item: the vendor's packages and their
    add-ons. The client sees only the public ones — a private package is the
    vendor's to offer, not the client's to ask for; the vendor sees every one
    that isn't archived."""
    q = db.query(Service).filter(Service.vendor_id == booking.vendor_id)
    q = q.filter(Service.status == "active") if side == "client" else q.filter(Service.status != "archived")
    return [
        {
            "service_id": s.service_id,
            "name": s.name,
            "price_cents": round((s.price or 0) * 100),
            "price_unit": s.price_unit,
            "add_ons": [
                {"id": a.get("id"), "name": a.get("name"), "price_cents": round((a.get("price") or 0) * 100)}
                for a in (s.add_ons or []) if a.get("id")
            ],
        }
        for s in q.order_by(Service.sort_order.is_(None), Service.sort_order, Service.name).all()
    ]


def _rows(db: Session, booking: Booking) -> list[NegotiationField]:
    return db.query(NegotiationField).filter(NegotiationField.booking_id == booking.booking_id).all()


def _turn(booking: Booking) -> str:
    return booking.negotiation_turn or "client"


def _round(booking: Booking) -> int:
    return booking.negotiation_round or 1


def _revision_terms(db: Session, booking: Booking, revision: int | None) -> dict | None:
    if not revision:
        return None
    row = db.query(ContractRevision).filter(
        ContractRevision.booking_id == booking.booking_id, ContractRevision.revision == revision,
    ).first()
    return row.terms if row else None


def _may_change(side: str, key: str, locks: list[str]) -> bool:
    group = group_of(key)
    if side == "vendor":
        return True
    if group == "schedule":
        return False
    return group not in locks


def state(booking: Booking, side: str, db: Session) -> dict:
    """Everything one side's workspace draws: the agreed terms, the version
    before and the original (for highlights), every field with where it
    stands, whose turn it is, and that side's draft."""
    if not is_fields(booking):
        raise FieldNegotiationError(409, "This contract uses the earlier way of proposing changes")
    terms = doc.terms(booking)
    current = fields_of(terms)
    rows = {r.field_key: r for r in _rows(db, booking)}
    locks = booking.negotiation_locks or []
    keys = list(current) + [k for k in rows if k not in current]

    fields = []
    for key in keys:
        r = rows.get(key)
        fields.append({
            "key": key,
            "group": group_of(key),
            "label": label(key, terms, r.proposed_value if r else None),
            "value": current.get(key),
            "state": r.state if r else "agreed",
            "proposed": r.proposed_value if r and r.state != SETTLED else None,
            "proposed_by": r.proposed_by if r and r.state != SETTLED else None,
            "round": r.round if r else None,
            "note": r.note if r else None,
            "locked": group_of(key) in locks,
            "can_change": _may_change(side, key, locks),
        })

    waiting = [f for f in fields if f["state"] in WAITING.values()]
    last = (
        db.query(NegotiationSend)
        .filter(NegotiationSend.booking_id == booking.booking_id)
        .order_by(NegotiationSend.round.desc())
        .first()
    )
    draft = db.query(ContractDraft).filter(
        ContractDraft.booking_id == booking.booking_id, ContractDraft.party == side,
    ).first()
    return {
        "mode": MODE,
        "side": side,
        "round": _round(booking),
        "turn": _turn(booking),
        "revision": booking.revision,
        "locks": locks,
        "nudge": _round(booking) >= NUDGE_ROUND,
        "can_sign": not waiting and booking.signed_at is None,
        "waiting_count": len(waiting),
        "terms": terms,
        "previous_terms": _revision_terms(db, booking, (booking.revision or 1) - 1),
        "original_terms": _revision_terms(db, booking, 1),
        "fields": fields,
        "last_send": {
            "round": last.round, "side": last.side, "message": last.message,
            "answers": last.answers, "sent_at": utc_iso(last.sent_at),
        } if last else None,
        "packages": packages_for(booking, side, db) if _turn(booking) == side else [],
        "draft": {
            "round": draft.base_revision, "answers": (draft.changes or {}).get("answers", []),
            "message": draft.message, "updated_at": utc_iso(draft.updated_at),
            "stale": draft.base_revision != _round(booking),
        } if draft else None,
    }


# ── Sending ──────────────────────────────────────────────────────────

def _require_open(booking: Booking) -> None:
    if not is_fields(booking):
        raise FieldNegotiationError(409, "This contract uses the earlier way of proposing changes")
    if booking.signed_at is not None:
        raise FieldNegotiationError(400, "This contract is signed, so it can't be changed now")
    if booking.contract_status in ("draft", "declined") or booking.status == "rejected":
        raise FieldNegotiationError(400, "This contract isn't open for changes")


def send(
    booking: Booking, vendor: Vendor, side: str, *,
    base_round: int, answers: list, message: str | None, db: Session,
) -> dict:
    """One side's turn: answer what's waiting on them, propose what they
    want changed, and pass the turn. All or nothing."""
    _require_open(booking)
    if side not in SIDES:
        raise FieldNegotiationError(400, "Unknown side")
    if _turn(booking) != side:
        raise FieldNegotiationError(409, f"It's the {_other(side)}'s turn — wait for their answer")
    if base_round != _round(booking):
        raise FieldNegotiationError(409, "The contract moved on since you opened it — review the latest round first")
    if not isinstance(answers, list) or not answers:
        raise FieldNegotiationError(400, "Answer or change something before sending")
    if len(answers) > MAX_ANSWERS:
        raise FieldNegotiationError(400, "That's too many changes for one send")

    terms = doc.terms(booking)
    current = fields_of(terms)
    rows = {r.field_key: r for r in _rows(db, booking)}
    locks = booking.negotiation_locks or []
    mine = WAITING[side]
    theirs = WAITING[_other(side)]
    seen: set[str] = set()
    settled: dict = {}
    record: list[dict] = []
    now = _now()
    next_round = _round(booking) + 1

    for a in answers:
        if not isinstance(a, dict):
            raise FieldNegotiationError(400, "Each answer is an object")
        key, action = a.get("key"), a.get("action")
        if not isinstance(key, str) or action not in ACTIONS:
            raise FieldNegotiationError(400, "Each answer needs a field and one of accept, counter, keep, change, reopen")
        if key in seen:
            raise FieldNegotiationError(400, f"{label(key, terms)} is answered twice")
        seen.add(key)
        group_of(key)  # an unknown key is refused here
        note = _text(a.get("note"), MAX_NOTE)
        r = rows.get(key)

        if action in ("accept", "counter", "keep"):
            if r is None or r.state != mine:
                raise FieldNegotiationError(400, f"{label(key, terms)} isn't waiting on you")
            if action == "accept":
                r.state, r.note = SETTLED, note
                settled[key] = r.proposed_value
            else:
                if action == "counter":
                    if side == "client" and group_of(key) == "schedule":
                        raise FieldNegotiationError(403, "The payment schedule is the vendor's to set — accept it or keep yours")
                    if not _may_change(side, key, locks):
                        raise FieldNegotiationError(403, f"{label(key, terms)} can't be changed")
                    value = validate(key, a.get("value"), terms, booking.vendor_id, db)
                else:
                    value = r.agreed_value
                r.state, r.proposed_value, r.proposed_by, r.note = theirs, value, side, note
            r.round, r.updated_at = next_round, now
            record.append({"key": key, "action": action, "value": r.proposed_value, "note": note})
            continue

        if action == "reopen":
            if side != "vendor":
                raise FieldNegotiationError(403, "Only the vendor can reopen a settled field")
            if r is None or r.state != SETTLED:
                raise FieldNegotiationError(400, f"{label(key, terms)} isn't settled")
        elif r is not None:
            raise FieldNegotiationError(400, f"{label(key, terms)} is already being negotiated")
        elif key not in current and not key.startswith("line:new:"):
            raise FieldNegotiationError(400, "That item or section isn't in the contract")
        if not _may_change(side, key, locks):
            raise FieldNegotiationError(403, f"{label(key, terms)} can't be changed")
        if side == "client" and key.startswith("line:new:"):
            offered = {p["service_id"]: p for p in packages_for(booking, "client", db)}
            ask = a.get("value") if isinstance(a.get("value"), dict) else {}
            pkg = offered.get(ask.get("service_id"))
            if pkg is None or (ask.get("addon_id") and ask["addon_id"] not in {x["id"] for x in pkg["add_ons"]}):
                raise FieldNegotiationError(400, "You can only add one of the vendor's listed packages or add-ons")
        if side == "client" and key.startswith("line:new:") and "prices" in locks:
            a = {**a, "value": {k: v for k, v in (a.get("value") or {}).items() if k != "unit_price_cents"}}
        value = validate(key, a.get("value"), terms, booking.vendor_id, db)
        if value == current.get(key):
            raise FieldNegotiationError(400, f"{label(key, terms)} is already {'that' if value is not None else 'empty'}")
        if r is None:
            r = NegotiationField(booking_id=booking.booking_id, field_key=key)
            db.add(r)
            rows[key] = r
        r.state, r.agreed_value, r.proposed_value, r.proposed_by = theirs, current.get(key), value, side
        r.round, r.note, r.updated_at = next_round, note, now
        record.append({"key": key, "action": action, "value": value, "note": note})

    left = [k for k, r in rows.items() if r.state == mine]
    if left:
        names = ", ".join(label(k, terms) for k in left[:3])
        raise FieldNegotiationError(400, f"Answer everything waiting on you first: {names}")

    _settle(booking, vendor, settled, db)
    booking.negotiation_round = next_round
    booking.negotiation_turn = _other(side)
    db.add(NegotiationSend(
        booking_id=booking.booking_id, round=next_round, side=side,
        message=_text(message, MAX_MESSAGE), answers=record, sent_at=now,
    ))
    db.query(ContractDraft).filter(
        ContractDraft.booking_id == booking.booking_id, ContractDraft.party == side,
    ).delete(synchronize_session=False)
    if side == "vendor":
        resend(booking, vendor, db)
    doc.record(db, booking, "negotiation_sent", side, {
        "round": next_round, "keys": [x["key"] for x in record],
        "settled": sorted(settled), "revision": booking.revision,
    })
    db.commit()
    db.refresh(booking)
    _tell(booking, vendor, side, record, db)
    return state(booking, side, db)


def _tell(booking: Booking, vendor: Vendor, side: str, record: list[dict], db: Session) -> None:
    """Let the other side know it's their turn. Best effort, after commit."""
    from app.services.guest_booking_service import _client_label, _tell_vendor

    n = len(record)
    what = f"{n} field{'s' if n != 1 else ''}"
    if side == "client":
        _tell_vendor(
            booking, f"{_client_label(booking)} answered your contract",
            f"Round {booking.negotiation_round}: {what} to review. It's your turn.",
            "contract_negotiation_turn", db,
        )
        return
    if not booking.guest_email:
        return
    who = _vendor_name(db.query(User).filter(User.user_id == vendor.user_id).first()) or "Your vendor"
    send_email(
        to=booking.guest_email,
        subject=f"{who} answered your changes",
        html=(
            f"<p>{who} answered {what} on your contract. It's your turn: accept, counter or keep each one, "
            f"or sign once nothing is waiting.</p>"
            f'<p><a href="{_link(booking)}">Open the contract</a></p>'
        ),
    )


# ── Signing (guest_booking_service.sign_contract) ────────────────────

def before_signing(booking: Booking, *, as_is: bool, db: Session) -> None:
    """Signing needs nothing waiting. Signing "as it is" takes the vendor's
    open changes and drops the client's own open asks, then signs that."""
    if not is_fields(booking):
        return
    rows = _rows(db, booking)
    waiting = [r for r in rows if r.state in WAITING.values()]
    if not waiting:
        return
    if not as_is:
        raise FieldNegotiationError(409, "Some changes are still waiting — answer them, or sign the contract as it is")
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    taken = {r.field_key: r.proposed_value for r in waiting if r.proposed_by == "vendor"}
    for r in waiting:
        if r.proposed_by == "vendor":
            r.state = SETTLED
        else:
            db.delete(r)
    _settle(booking, vendor, taken, db)
    doc.record(db, booking, "negotiation_signed_as_is", "client", {
        "taken": sorted(taken), "dropped": sorted(r.field_key for r in waiting if r.proposed_by == "client"),
    })


def has_waiting(booking: Booking, db: Session) -> bool:
    return is_fields(booking) and db.query(NegotiationField).filter(
        NegotiationField.booking_id == booking.booking_id,
        NegotiationField.state.in_(tuple(WAITING.values())),
    ).first() is not None


# ── Drafts ───────────────────────────────────────────────────────────

def save_draft(booking: Booking, side: str, *, answers: list, message: str | None, db: Session) -> dict:
    """What one side has typed so far: shape-checked only, like the 0069
    drafts it shares a table with. `base_revision` holds the round."""
    _require_open(booking)
    if not isinstance(answers, list):
        raise FieldNegotiationError(400, "Send the answers as a list")
    if len(json.dumps(answers, default=str)) > MAX_DRAFT_BYTES:
        raise FieldNegotiationError(413, "This draft is too large to save")
    d = db.query(ContractDraft).filter(ContractDraft.booking_id == booking.booking_id, ContractDraft.party == side).first()
    if d is None:
        d = ContractDraft(booking_id=booking.booking_id, party=side)
        db.add(d)
    d.base_revision = _round(booking)
    d.changes = {"answers": answers}
    d.message = _text(message, MAX_MESSAGE)
    d.updated_at = _now()
    db.commit()
    return state(booking, side, db)["draft"]


def drop_draft(booking: Booking, side: str, db: Session) -> None:
    db.query(ContractDraft).filter(
        ContractDraft.booking_id == booking.booking_id, ContractDraft.party == side,
    ).delete(synchronize_session=False)
    db.commit()


# ── Entry points by side ─────────────────────────────────────────────

def _vendor_booking(booking_id: str, caller_user_id: str, db: Session) -> tuple[Booking, Vendor]:
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking or not booking.contract_token:
        raise FieldNegotiationError(404, "Contract not found")
    return booking, _own_vendor(vendor_id=booking.vendor_id, caller_user_id=caller_user_id, db=db)


def _client_booking(contract_token: str, db: Session) -> tuple[Booking, Vendor]:
    from app.services.guest_booking_service import _by_token, _require_live

    booking = _by_token(contract_token, db)
    _require_live(booking)
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    return booking, vendor


def vendor_state(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    booking, _ = _vendor_booking(booking_id, caller_user_id, db)
    return state(booking, "vendor", db)


def vendor_send(*, booking_id: str, caller_user_id: str, base_round: int, answers: list, message: str | None, db: Session) -> dict:
    booking, vendor = _vendor_booking(booking_id, caller_user_id, db)
    return send(booking, vendor, "vendor", base_round=base_round, answers=answers, message=message, db=db)


def vendor_save_draft(*, booking_id: str, caller_user_id: str, answers: list, message: str | None, db: Session) -> dict:
    booking, _ = _vendor_booking(booking_id, caller_user_id, db)
    return save_draft(booking, "vendor", answers=answers, message=message, db=db)


def vendor_drop_draft(*, booking_id: str, caller_user_id: str, db: Session) -> None:
    booking, _ = _vendor_booking(booking_id, caller_user_id, db)
    drop_draft(booking, "vendor", db)


def client_state(*, contract_token: str, db: Session) -> dict:
    from app.services.guest_booking_service import _by_token

    return state(_by_token(contract_token, db), "client", db)


def client_send(*, contract_token: str, base_round: int, answers: list, message: str | None, db: Session) -> dict:
    from app.services.guest_booking_service import (
        GuestBookingError,
        _require_open_offer,
    )

    booking, vendor = _client_booking(contract_token, db)
    _require_open_offer(booking)
    if not booking.guest_email:
        raise GuestBookingError(400, "Add your email first so we can tell you when they reply")
    return send(booking, vendor, "client", base_round=base_round, answers=answers, message=message, db=db)


def client_save_draft(*, contract_token: str, answers: list, message: str | None, db: Session) -> dict:
    booking, _ = _client_booking(contract_token, db)
    return save_draft(booking, "client", answers=answers, message=message, db=db)


def client_drop_draft(*, contract_token: str, db: Session) -> None:
    from app.services.guest_booking_service import _by_token

    drop_draft(_by_token(contract_token, db), "client", db)
