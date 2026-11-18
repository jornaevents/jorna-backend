"""Propose a detail in chat (DECISIONS #59): either side of a client–vendor
thread proposes one event detail — a date, the times, a guest count, or one
of the vendor's add-ons — and the other agrees or declines. An agreed value
is exact, so the contract can take it as said: what makes chat-to-contract
reliable without a model reading the messages.

A proposal is a message of kind "detail_proposal" whose meta carries the
field, the value, a label to show, and its status: open, agreed, declined,
or replaced (a newer proposal for the same field took its place). Only one
proposal per field is open at a time. The text says it all too, for apps
that don't draw the card.
"""

import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import CLIENT_WEB_APP_URL
from app.db.models import Conversation, GroupMessage, Service, User, Vendor
from app.services.conversation_service import (
    ConversationError,
    _assert_is_member,
    _message_dict,
    send_group_message,
)
from app.services.names import vendor_user_display_name

KIND = "detail_proposal"
FIELDS = ("date", "times", "guests", "addon")
OPEN, AGREED, DECLINED, REPLACED = "open", "agreed", "declined", "replaced"

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clock(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    return f"{h % 12 or 12}:{m:02d} {'PM' if h >= 12 else 'AM'}"


def _thread(conversation_id: str, caller_user_id: str, db: Session) -> tuple[Conversation, Vendor]:
    _assert_is_member(conversation_id, caller_user_id, db)
    conv = db.query(Conversation).filter(Conversation.conversation_id == conversation_id).first()
    if not conv:
        raise ConversationError(404, "Conversation not found")
    vendor = db.query(Vendor).filter(Vendor.vendor_id == conv.vendor_id).first() if conv.vendor_id else None
    if vendor is None or not conv.client_user_id:
        raise ConversationError(400, "Details are proposed in a chat between a client and a vendor")
    if caller_user_id not in (vendor.user_id, conv.client_user_id):
        raise ConversationError(403, "Only the client and the vendor can propose details here")
    return conv, vendor


def _checked(field: str, value, vendor: Vendor, db: Session) -> tuple[str, str, dict]:
    """The value as stored, its label, and anything extra the card needs.
    Checked as the contract checks them, so an agreed value always fits."""
    if field == "date":
        text = str(value or "")
        try:
            day = datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            raise ConversationError(422, "Pick a date")
        if day < _now().date():
            raise ConversationError(422, "That date has already passed")
        return text, day.strftime("%a, %b %-d, %Y"), {}
    if field == "times":
        start, _, end = str(value or "").partition("-")
        if not (_TIME.match(start) and _TIME.match(end)):
            raise ConversationError(422, "Give a start and an end time")
        return f"{start}-{end}", f"{_clock(start)} – {_clock(end)}", {}
    if field == "guests":
        try:
            count = int(str(value))
        except ValueError:
            raise ConversationError(422, "Guest count must be a whole number")
        if not 1 <= count <= 100_000:
            raise ConversationError(422, "Guest count must be between 1 and 100,000")
        return str(count), f"{count:,} guests", {}
    if field == "addon":
        for service in db.query(Service).filter(Service.vendor_id == vendor.vendor_id, Service.status != "archived").all():
            for addon in service.add_ons or []:
                if addon.get("id") and addon.get("id") == value:
                    extra = {
                        "addon": {
                            "id": addon["id"], "name": addon.get("name"), "price": addon.get("price"),
                            "price_unit": addon.get("price_unit"), "service_id": service.service_id,
                        }
                    }
                    return str(value), str(addon.get("name") or "Add-on"), extra
        raise ConversationError(422, "That add-on isn't one of the vendor's")
    raise ConversationError(422, f"Details are one of: {', '.join(FIELDS)}")


_NOUN = {"date": "a date", "times": "the times", "guests": "a guest count", "addon": "an add-on"}


def propose(*, conversation_id: str, caller_user_id: str, field: str, value, db: Session) -> dict:
    conv, vendor = _thread(conversation_id, caller_user_id, db)
    stored, label, extra = _checked(field, value, vendor, db)

    # One open proposal per field: the newer one takes its place.
    for old in db.query(GroupMessage).filter(
        GroupMessage.conversation_id == conversation_id, GroupMessage.kind == KIND,
    ).all():
        meta = old.meta or {}
        if meta.get("field") == field and meta.get("status") == OPEN:
            old.meta = {**meta, "status": REPLACED}
    db.commit()

    caller = db.query(User).filter(User.user_id == caller_user_id).first()
    who = vendor_user_display_name(caller, "Someone") if caller_user_id == vendor.user_id else (
        (caller.f_name or "").strip() if caller else "") or "Your client"
    link = f"{CLIENT_WEB_APP_URL.rstrip('/')}/conversation/?id={conversation_id}"
    content = f"{who} proposed {_NOUN[field]}: {label}. Agree or decline it in the chat on Jorna: {link}"
    return send_group_message(
        conversation_id=conversation_id, content=content, caller_user_id=caller_user_id, db=db,
        kind=KIND,
        meta={"field": field, "value": stored, "label": label, "status": OPEN, "proposed_by": caller_user_id, **extra},
    )


def answer(*, conversation_id: str, message_id: str, caller_user_id: str, agree: bool, db: Session) -> dict:
    """The other side's answer. Only while it's open, and never by whoever
    proposed it — agreeing with yourself isn't an agreement."""
    _thread(conversation_id, caller_user_id, db)
    msg = db.query(GroupMessage).filter(
        GroupMessage.message_id == message_id, GroupMessage.conversation_id == conversation_id,
        GroupMessage.kind == KIND,
    ).first()
    if msg is None:
        raise ConversationError(404, "Proposal not found")
    meta = msg.meta or {}
    if meta.get("proposed_by") == caller_user_id:
        raise ConversationError(403, "The other side answers a proposal")
    if meta.get("status") != OPEN:
        raise ConversationError(409, "This proposal has already been answered or replaced")
    msg.meta = {
        **meta, "status": AGREED if agree else DECLINED,
        "answered_by": caller_user_id, "answered_at": _now().isoformat(),
    }
    db.commit()
    db.refresh(msg)
    sender = db.query(User).filter(User.user_id == msg.sender_id).first() if msg.sender_id else None
    return _message_dict(msg, sender, [])
