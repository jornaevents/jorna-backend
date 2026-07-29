"""Guest lists, the functions they're invited to, and what they said.

A celebration here is usually several gatherings — a mehndi, a sangeet, a
ceremony, a reception — with different guest lists and separate per-person
catering. So an invitation is per function, and the headcount that matters is
per function too: that is the number a caterer bills against.

Nothing in here touches money. It reports what the replies add up to and leaves
the decision to the host, because a vendor booked for two hundred is holding a
promise, not a variable.
"""

import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Event, EventFunction, Guest, GuestInvite

logger = logging.getLogger(__name__)

REPLY_STATUSES = ("no_reply", "attending", "declined")


class GuestError(Exception):
    """Raised when a guest-list operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _token() -> str:
    """A link's whole credential, so it has to be unguessable.

    Not derived from any id: a token you can compute from a guest's id is a
    token every other guest can compute too.
    """
    return secrets.token_urlsafe(24)


def _own_event(event_id: str, user_id: str, db: Session) -> Event:
    event = db.query(Event).filter(Event.event_id == event_id).first()
    if not event:
        raise GuestError(404, "Event not found")
    if event.user_id != user_id:
        raise GuestError(403, "Not your celebration")
    return event


# ── Functions ─────────────────────────────────────────────────────────


def _default_function(event: Event, db: Session) -> EventFunction:
    """The one function a celebration has before anybody names its parts.

    A guest list needs something to invite people to, and most celebrations
    start as a single gathering. Named after the event, so a host who never
    thinks about functions never sees the word.
    """
    fn = EventFunction(
        event_id=event.event_id,
        name=event.name or "The celebration",
        date_iso=event.date_iso,
        location=event.location,
        sort_order=0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(fn)
    db.commit()
    db.refresh(fn)
    return fn


def list_functions(
    *, event_id: str, user_id: str, db: Session, create_default: bool = False,
) -> list[dict]:
    """The celebration's functions.

    `create_default` is off for reads. Opening a plan shouldn't write a row to
    the database, and an event that has no guest list yet genuinely has no
    functions — that's what the UI needs to know to offer starting one. The
    default is created by the first act that needs something to invite people
    to: adding a guest, or minting the shared link.
    """
    event = _own_event(event_id, user_id, db)
    rows = (
        db.query(EventFunction)
        .filter(EventFunction.event_id == event_id)
        .order_by(EventFunction.sort_order, EventFunction.created_at)
        .all()
    )
    if not rows and create_default:
        rows = [_default_function(event, db)]
    return [_function_dict(f) for f in rows]


def add_function(
    *, event_id: str, user_id: str, name: str, date_iso: str | None,
    time_start: str | None, time_end: str | None, location: str | None, db: Session,
) -> dict:
    _own_event(event_id, user_id, db)
    if not (name or "").strip():
        raise GuestError(400, "A function needs a name")

    highest = (
        db.query(EventFunction)
        .filter(EventFunction.event_id == event_id)
        .order_by(EventFunction.sort_order.desc())
        .first()
    )
    fn = EventFunction(
        event_id=event_id,
        name=name.strip(),
        date_iso=date_iso or None,
        time_start=time_start or None,
        time_end=time_end or None,
        location=location or None,
        sort_order=(highest.sort_order + 1) if highest else 0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(fn)
    db.commit()
    db.refresh(fn)
    return _function_dict(fn)


def update_function(*, function_id: str, user_id: str, updates: dict, db: Session) -> dict:
    fn = db.query(EventFunction).filter(EventFunction.function_id == function_id).first()
    if not fn:
        raise GuestError(404, "Function not found")
    _own_event(fn.event_id, user_id, db)

    for field in ("name", "date_iso", "time_start", "time_end", "location"):
        if field in updates and updates[field] is not None:
            setattr(fn, field, updates[field] or None)
    db.commit()
    db.refresh(fn)
    return _function_dict(fn)


def delete_function(*, function_id: str, user_id: str, db: Session) -> None:
    """Remove a function, and the invitations to it.

    The guests stay — they're invited to the celebration, and losing a whole
    guest list because a function was renamed by deletion would be its own kind
    of disaster.
    """
    fn = db.query(EventFunction).filter(EventFunction.function_id == function_id).first()
    if not fn:
        raise GuestError(404, "Function not found")
    _own_event(fn.event_id, user_id, db)

    remaining = (
        db.query(EventFunction)
        .filter(EventFunction.event_id == fn.event_id, EventFunction.function_id != function_id)
        .count()
    )
    if remaining == 0:
        raise GuestError(400, "A celebration needs at least one function to invite people to")

    db.query(GuestInvite).filter(GuestInvite.function_id == function_id).delete()
    db.delete(fn)
    db.commit()


def _function_dict(fn: EventFunction) -> dict:
    return {
        "function_id": fn.function_id,
        "event_id": fn.event_id,
        "name": fn.name,
        "date_iso": fn.date_iso,
        "time_start": fn.time_start,
        "time_end": fn.time_end,
        "location": fn.location,
        "sort_order": fn.sort_order,
    }


# ── Guests ────────────────────────────────────────────────────────────


def _set_invites(guest: Guest, function_ids: list[str], db: Session) -> None:
    """Make the guest's invitations exactly the functions named.

    Replies already given are kept: removing somebody from the sangeet and
    putting them back shouldn't lose the fact that they said yes.
    """
    existing = {
        inv.function_id: inv
        for inv in db.query(GuestInvite).filter(GuestInvite.guest_id == guest.guest_id).all()
    }
    wanted = set(function_ids)

    for function_id in wanted - set(existing):
        db.add(GuestInvite(guest_id=guest.guest_id, function_id=function_id, status="no_reply"))
    for function_id in set(existing) - wanted:
        db.delete(existing[function_id])


def add_guest(
    *, event_id: str, user_id: str, name: str, email: str | None, phone: str | None,
    party_size: int, function_ids: list[str], note: str | None, db: Session,
) -> dict:
    event = _own_event(event_id, user_id, db)
    if not (name or "").strip():
        raise GuestError(400, "A guest needs a name")

    functions = _valid_function_ids(event_id, function_ids, db)
    if not functions:
        # Nobody is invited to nothing. Falling back to every function is what a
        # host means by adding a guest without saying which parts — and this is
        # the moment a celebration with no functions gets its first one.
        functions = [
            f["function_id"]
            for f in list_functions(
                event_id=event_id, user_id=user_id, db=db, create_default=True
            )
        ]

    guest = Guest(
        event_id=event.event_id,
        name=name.strip(),
        email=(email or "").strip() or None,
        phone=(phone or "").strip() or None,
        party_size=max(1, int(party_size or 1)),
        token=_token(),
        note=(note or "").strip() or None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(guest)
    db.flush()
    _set_invites(guest, functions, db)
    db.commit()
    db.refresh(guest)
    return _guest_dict(guest, db)


def add_guests_bulk(
    *, event_id: str, user_id: str, lines: list[str], function_ids: list[str], db: Session,
) -> dict:
    """Add many at once from pasted lines.

    Each line is a name, optionally followed by an email and a party size in any
    order — "Anita Sharma, anita@example.com, 3". Lines that are only whitespace
    are skipped rather than becoming blank guests.
    """
    added = []
    for raw in lines:
        parts = [p.strip() for p in (raw or "").replace("\t", ",").split(",")]
        parts = [p for p in parts if p]
        if not parts:
            continue

        name = parts[0]
        email = next((p for p in parts[1:] if "@" in p), None)
        size = next((int(p) for p in parts[1:] if p.isdigit()), 1)
        added.append(
            add_guest(
                event_id=event_id, user_id=user_id, name=name, email=email, phone=None,
                party_size=size, function_ids=function_ids, note=None, db=db,
            )
        )
    return {"added": len(added), "guests": added}


def update_guest(*, guest_id: str, user_id: str, updates: dict, db: Session) -> dict:
    guest = db.query(Guest).filter(Guest.guest_id == guest_id).first()
    if not guest:
        raise GuestError(404, "Guest not found")
    _own_event(guest.event_id, user_id, db)

    for field in ("name", "email", "phone", "note"):
        if field in updates and updates[field] is not None:
            setattr(guest, field, str(updates[field]).strip() or None)
    if updates.get("party_size") is not None:
        guest.party_size = max(1, int(updates["party_size"]))
    if updates.get("function_ids") is not None:
        wanted = _valid_function_ids(guest.event_id, updates["function_ids"], db)
        if not wanted:
            # A guest invited to nothing isn't on the list at all — they just stop
            # counting anywhere without disappearing. Say so instead.
            raise GuestError(400, "A guest has to be invited to at least one function")
        _set_invites(guest, wanted, db)

    db.commit()
    db.refresh(guest)
    return _guest_dict(guest, db)


def delete_guest(*, guest_id: str, user_id: str, db: Session) -> None:
    guest = db.query(Guest).filter(Guest.guest_id == guest_id).first()
    if not guest:
        raise GuestError(404, "Guest not found")
    _own_event(guest.event_id, user_id, db)
    db.query(GuestInvite).filter(GuestInvite.guest_id == guest_id).delete()
    db.delete(guest)
    db.commit()


def _valid_function_ids(event_id: str, function_ids: list[str], db: Session) -> list[str]:
    """Only functions of this celebration — a guest can't be invited elsewhere."""
    if not function_ids:
        return []
    rows = (
        db.query(EventFunction.function_id)
        .filter(
            EventFunction.event_id == event_id,
            EventFunction.function_id.in_(function_ids),
        )
        .all()
    )
    return [r.function_id for r in rows]


def _guest_dict(guest: Guest, db: Session) -> dict:
    invites = db.query(GuestInvite).filter(GuestInvite.guest_id == guest.guest_id).all()
    return {
        "guest_id": guest.guest_id,
        "name": guest.name,
        "email": guest.email,
        "phone": guest.phone,
        "party_size": guest.party_size,
        "note": guest.note,
        "self_added": guest.self_added,
        "token": guest.token,
        "invites": [
            {
                "function_id": i.function_id,
                "status": i.status,
                "attending_count": i.attending_count,
                "responded_at": i.responded_at.isoformat() if i.responded_at else None,
            }
            for i in invites
        ],
    }


def list_guests(*, event_id: str, user_id: str, db: Session) -> dict:
    """The list, its functions, and what the replies add up to per function."""
    event = _own_event(event_id, user_id, db)
    functions = list_functions(event_id=event_id, user_id=user_id, db=db)
    guests = (
        db.query(Guest)
        .filter(Guest.event_id == event_id)
        .order_by(Guest.created_at)
        .all()
    )
    return {
        "functions": functions,
        "guests": [_guest_dict(g, db) for g in guests],
        "headcount": headcount(event_id=event_id, db=db),
        # Whether a shared link is already out there. Without this the UI can
        # only offer to make one, and would go on offering after a reload — a
        # host would have no way to tell a live link from none at all.
        "invite_link": event.invite_token,
    }


def headcount(*, event_id: str, db: Session) -> list[dict]:
    """Per function: who's coming, who isn't, who hasn't said.

    `attending` counts people, not invitations — an attending_count where one is
    given, otherwise the party size the host expected, because a yes without a
    number still means at least the people you invited.
    """
    functions = (
        db.query(EventFunction)
        .filter(EventFunction.event_id == event_id)
        .order_by(EventFunction.sort_order, EventFunction.created_at)
        .all()
    )
    out = []
    for fn in functions:
        rows = (
            db.query(GuestInvite, Guest)
            .join(Guest, Guest.guest_id == GuestInvite.guest_id)
            .filter(GuestInvite.function_id == fn.function_id)
            .all()
        )
        attending = sum(
            (inv.attending_count if inv.attending_count is not None else guest.party_size)
            for inv, guest in rows
            if inv.status == "attending"
        )
        out.append({
            "function_id": fn.function_id,
            "name": fn.name,
            "date_iso": fn.date_iso,
            "invited": len(rows),
            "attending": attending,
            "declined": sum(1 for inv, _ in rows if inv.status == "declined"),
            "no_reply": sum(1 for inv, _ in rows if inv.status == "no_reply"),
            # What the host expects if everyone says yes — the figure a plan is
            # usually built against before anybody has answered.
            "expected": sum(guest.party_size for _, guest in rows),
        })
    return out


# ── The open link ─────────────────────────────────────────────────────


def open_link_token(*, event_id: str, user_id: str, db: Session) -> dict:
    """The celebration's shareable link, minted on first ask.

    One link for a family group chat, where whoever opens it adds themselves.
    It's the way these lists actually get worked, and the cost is that anybody
    holding the link can add to a headcount — so it's only ever created when a
    host asks for it, and it can be retired.
    """
    event = _own_event(event_id, user_id, db)
    # There has to be something to RSVP to before the link goes out — minting it
    # is the last moment we can still create the default function without doing
    # it on a stranger's GET.
    list_functions(event_id=event_id, user_id=user_id, db=db, create_default=True)
    if not event.invite_token:
        event.invite_token = _token()
        db.commit()
    return {"token": event.invite_token}


def retire_open_link(*, event_id: str, user_id: str, db: Session) -> dict:
    """Stop the shared link working. Guests already added keep their replies."""
    event = _own_event(event_id, user_id, db)
    event.invite_token = None
    db.commit()
    return {"token": None}


# ── What a guest sees, and what they say ──────────────────────────────
#
# These are the only endpoints with no login behind them. The token is the whole
# credential, so what they return is bounded by what an invitation should say:
# the celebration, its functions, and the reader's own name. Never the guest
# list, never another guest, never anything about the host's vendors or money.


def _invitation(event: Event, functions: list[EventFunction]) -> dict:
    return {
        "event_name": event.name,
        "functions": [_function_dict(f) for f in functions],
        # The address the host settled on — the venue's own, once one is booked.
        "location": event.location,
        "date_iso": event.date_iso,
    }


def invitation_for_guest(*, token: str, db: Session) -> dict:
    """The invitation a per-guest link opens.

    Includes what they've already said, so arriving a second time shows their
    answer rather than asking again as though it never happened.
    """
    guest = db.query(Guest).filter(Guest.token == token).first()
    if not guest:
        raise GuestError(404, "This invitation link isn't valid")

    event = db.query(Event).filter(Event.event_id == guest.event_id).first()
    if not event:
        raise GuestError(404, "This celebration no longer exists")

    invites = {
        inv.function_id: inv
        for inv in db.query(GuestInvite).filter(GuestInvite.guest_id == guest.guest_id).all()
    }
    functions = (
        db.query(EventFunction)
        .filter(EventFunction.function_id.in_(list(invites.keys())))
        .order_by(EventFunction.sort_order, EventFunction.created_at)
        .all()
    ) if invites else []

    payload = _invitation(event, functions)
    payload["guest"] = {
        "name": guest.name,
        "party_size": guest.party_size,
        "replies": [
            {
                "function_id": fid,
                "status": inv.status,
                "attending_count": inv.attending_count,
            }
            for fid, inv in invites.items()
        ],
    }
    return payload


def record_reply(*, token: str, replies: list[dict], db: Session) -> dict:
    """Record a guest's answers.

    Deliberately a write and nothing else — the link that opens the invitation
    only reads. Anything that changes an answer has to be asked for, so a mail
    scanner or a link preview can't RSVP on somebody's behalf.
    """
    guest = db.query(Guest).filter(Guest.token == token).first()
    if not guest:
        raise GuestError(404, "This invitation link isn't valid")

    existing = {
        inv.function_id: inv
        for inv in db.query(GuestInvite).filter(GuestInvite.guest_id == guest.guest_id).all()
    }
    now = datetime.now(timezone.utc)

    for reply in replies:
        invite = existing.get(reply.get("function_id"))
        if invite is None:
            # Not invited to that one. Silently ignored rather than refused: the
            # reply that matters is the rest of them.
            continue
        status = reply.get("status")
        if status not in ("attending", "declined"):
            continue

        invite.status = status
        invite.responded_at = now
        if status == "attending":
            count = reply.get("attending_count")
            # A yes without a number still means the people who were invited.
            invite.attending_count = (
                max(1, int(count)) if count is not None else guest.party_size
            )
        else:
            invite.attending_count = 0

    db.commit()
    logger.info("RSVP recorded for guest %s", guest.guest_id)
    return invitation_for_guest(token=token, db=db)


def invitation_for_open_link(*, token: str, db: Session) -> dict:
    """The invitation a shared link opens, before anybody has said who they are."""
    event = db.query(Event).filter(Event.invite_token == token).first()
    if not event:
        raise GuestError(404, "This invitation link isn't valid")

    functions = (
        db.query(EventFunction)
        .filter(EventFunction.event_id == event.event_id)
        .order_by(EventFunction.sort_order, EventFunction.created_at)
        .all()
    )
    payload = _invitation(event, functions)
    payload["guest"] = None
    return payload


def join_via_open_link(
    *, token: str, name: str, email: str | None, party_size: int,
    replies: list[dict], db: Session,
) -> dict:
    """Add yourself to a guest list from the shared link, and answer.

    Marked self_added, because a headcount that grew on its own is worth being
    able to tell from one the host built. Returns the new guest's own token, so
    from here on they hold a normal invitation they can come back to and change.
    """
    event = db.query(Event).filter(Event.invite_token == token).first()
    if not event:
        raise GuestError(404, "This invitation link isn't valid")
    if not (name or "").strip():
        raise GuestError(400, "Please give your name so the host knows who's coming")

    guest = Guest(
        event_id=event.event_id,
        name=name.strip(),
        email=(email or "").strip() or None,
        party_size=max(1, int(party_size or 1)),
        token=_token(),
        self_added=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(guest)
    db.flush()

    # Invited to everything the shared link covers, not only the functions they
    # got round to answering — somebody who replies to the sangeet and skips the
    # reception is undecided about the reception, not uninvited from it.
    all_functions = (
        db.query(EventFunction.function_id)
        .filter(EventFunction.event_id == event.event_id)
        .all()
    )
    _set_invites(guest, [f.function_id for f in all_functions], db)
    db.commit()

    return record_reply(token=guest.token, replies=replies, db=db) | {"token": guest.token}
