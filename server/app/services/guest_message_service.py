"""Messages between a vendor and the client on the contract link (DECISIONS #41).

A client who only has the contract link — no account — can still ask "can we
start an hour later?". They write into the booking's ordinary thread, which the
vendor reads and answers in Messages like any other; the link is the whole
credential, the same trust model as everything else on /guest-bookings.

- A client with an account who opens their link writes as themselves, into the
  same thread they see in the app.
- One without has no user to be: their messages carry no sender_id, and the
  booking names them (client_label). They aren't a member of the thread, so
  their read state lives on the booking, and the vendor's replies reach them
  by email — one per stretch of unread replies, not one per message.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from html import escape

from sqlalchemy.orm import Session

from app.config import WEB_APP_URL
from app.db.models import (
    Booking,
    Conversation,
    GroupMessage,
    GroupMessageRead,
    User,
    Vendor,
)
from app.services.guest_booking_service import (
    GuestBookingError,
    _by_token,
    _require_live,
)
from app.services.names import vendor_display_name

logger = logging.getLogger(__name__)

MAX_LENGTH = 4000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def client_label(booking: Booking) -> str:
    """What the vendor sees the link client called."""
    return booking.guest_name or booking.signer_name or "Your client"


def thread_client_label(conversation_id: str, db: Session) -> str | None:
    """client_label for a thread's booking, for naming its senderless messages."""
    conv = db.query(Conversation).filter(Conversation.conversation_id == conversation_id).first()
    booking = (
        db.query(Booking).filter(Booking.booking_id == conv.booking_id).first()
        if conv and conv.booking_id else None
    )
    return client_label(booking) if booking else "Your client"


def _parties(booking: Booking, db: Session) -> tuple[Vendor, User]:
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    vendor_user = db.query(User).filter(User.user_id == vendor.user_id).first() if vendor else None
    if not vendor or not vendor_user:
        raise GuestBookingError(404, "Booking not found")
    return vendor, vendor_user


def _thread(booking: Booking, vendor_user: User, db: Session) -> Conversation:
    from app.services.conversation_service import booking_thread

    return booking_thread(booking, vendor_user, db)


def _item(msg: GroupMessage, booking: Booking, vendor_name: str) -> dict:
    mine = msg.sender_id is None or msg.sender_id == booking.user_id
    return {
        "message_id": msg.message_id,
        "from": "you" if mine else "vendor",
        "sender_name": client_label(booking) if mine else vendor_name,
        "content": msg.content,
        "kind": msg.kind,
        "created_at": msg.created_at.isoformat(),
    }


def list_messages(*, contract_token: str, db: Session) -> dict:
    """The booking's thread as the link client sees it, oldest first. Opening
    it is reading it."""
    booking = _by_token(contract_token, db)
    vendor, vendor_user = _parties(booking, db)
    conv = _thread(booking, vendor_user, db)
    messages = (
        db.query(GroupMessage)
        .filter(GroupMessage.conversation_id == conv.conversation_id)
        .order_by(GroupMessage.created_at.asc())
        .limit(200)
        .all()
    )
    now = _now()
    if booking.user_id:
        # They're a member: read the way the app reads (get_group_messages).
        read = {
            r.message_id for r in db.query(GroupMessageRead).filter(
                GroupMessageRead.user_id == booking.user_id,
                GroupMessageRead.message_id.in_([m.message_id for m in messages]),
            ).all()
        }
        for m in messages:
            if m.message_id not in read:
                db.add(GroupMessageRead(message_id=m.message_id, user_id=booking.user_id, read_at=now))
    else:
        booking.guest_messages_read_at = now.replace(tzinfo=None)
    db.commit()
    vendor_name = vendor_display_name(vendor, vendor_user, "Your vendor")
    return {
        "vendor_name": vendor_name,
        "items": [_item(m, booking, vendor_name) for m in messages],
    }


def post_message(*, contract_token: str, content: str, db: Session) -> tuple[str, dict]:
    """The link client writes to their vendor. Returns the thread's id and the
    message as the vendor's side sees it, for the live push to an open thread."""
    booking = _by_token(contract_token, db)
    _require_live(booking)
    text = (content or "").strip()
    if not text:
        raise GuestBookingError(400, "Write a message first")
    if len(text) > MAX_LENGTH:
        raise GuestBookingError(400, f"Keep it under {MAX_LENGTH} characters")
    _vendor, vendor_user = _parties(booking, db)
    conv = _thread(booking, vendor_user, db)

    if booking.user_id:
        # Their own account's message, as if sent from the app.
        from app.services.conversation_service import (
            ConversationError,
            send_group_message,
        )

        try:
            sent = send_group_message(
                conversation_id=conv.conversation_id, content=text,
                caller_user_id=booking.user_id, db=db,
            )
        except ConversationError as e:
            raise GuestBookingError(e.status_code, e.detail)
    else:
        from app.services.conversation_service import _message_dict

        now = _now()
        msg = GroupMessage(
            conversation_id=conv.conversation_id, sender_id=None, content=text,
            created_at=now, kind="text",
        )
        db.add(msg)
        # Writing is reading: what came before this is seen.
        booking.guest_messages_read_at = now.replace(tzinfo=None)
        db.commit()
        try:
            from app.utils.notifications import send_push_to_user

            send_push_to_user(
                vendor_user,
                f"New message from {client_label(booking)}",
                text[:100],
                {"conversation_id": conv.conversation_id, "type": "group_message"},
                db=db,
            )
        except Exception as exc:
            logger.warning("Link client message push failed: %s", exc)
        sent = _message_dict(msg, None, [], client_label(booking))
    return conv.conversation_id, sent


def email_link_client(conversation_id: str, db: Session) -> bool:
    """After the vendor writes in a booking's thread: email the client on the
    link, if they have no account (an account holder is a member and gets the
    app's own push and digest) and haven't already been emailed about replies
    they still haven't read. Returns whether an email went."""
    conv = db.query(Conversation).filter(Conversation.conversation_id == conversation_id).first()
    if not conv or not conv.booking_id:
        return False
    booking = db.query(Booking).filter(Booking.booking_id == conv.booking_id).first()
    if not booking or booking.user_id or not booking.guest_email or not booking.contract_token:
        return False
    if booking.guest_message_emailed_at and (
        booking.guest_messages_read_at is None
        or booking.guest_message_emailed_at > booking.guest_messages_read_at
    ):
        return False  # already told; they haven't been back since

    from app.services.email_service import send_email

    vendor, vendor_user = _parties(booking, db)
    vendor_name = vendor_display_name(vendor, vendor_user, "Your vendor")
    link = f"{WEB_APP_URL}/booking-link?t={booking.contract_token}#messages"
    result = send_email(
        to=booking.guest_email,
        subject=f"{vendor_name} sent you a message",
        html=(
            f"<p>{escape(vendor_name)} sent you a message about your booking.</p>"
            f'<p><a href="{link}">Read and reply</a></p>'
        ),
        text=f"{vendor_name} sent you a message about your booking.\n\nRead and reply: {link}\n",
    )
    if result.get("success"):
        booking.guest_message_emailed_at = _now().replace(tzinfo=None)
        db.commit()
        return True
    return False
