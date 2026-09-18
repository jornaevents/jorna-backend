"""Business logic for booking-scoped messaging."""

from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_

from app.db.models import Booking, Message, User, Vendor


class MessageError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _get_booking_parties(booking: Booking, db: Session) -> tuple[str, str]:
    """Return (client_user_id, vendor_user_id) for a booking."""
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    if not vendor:
        raise MessageError(404, "Vendor not found for this booking")
    return booking.user_id, vendor.user_id


def _assert_is_party(booking: Booking, caller_user_id: str, db: Session) -> str:
    """Raise 403 if caller is not a party. Returns the receiver's user_id."""
    client_id, vendor_user_id = _get_booking_parties(booking, db)
    if caller_user_id == client_id:
        return vendor_user_id
    if caller_user_id == vendor_user_id:
        return client_id
    raise MessageError(403, "You are not a party to this booking")


def _message_dict(msg: Message, sender: User | None = None) -> dict:
    return {
        "message_id": msg.message_id,
        "booking_id": msg.booking_id,
        "sender_id": msg.sender_id,
        "receiver_id": msg.receiver_id,
        "content": msg.content,
        "created_at": msg.created_at.isoformat(),
        "is_read": msg.is_read,
        "sender_name": f"{sender.f_name} {sender.l_name}" if sender else None,
        "sender_pfp": sender.pfp_url if sender else None,
    }


def send_message(
    *, booking_id: str, content: str, caller_user_id: str, db: Session
) -> dict:
    """Send a message on a booking conversation. Caller must be the client or vendor."""
    if not content.strip():
        raise MessageError(400, "Message content cannot be empty")

    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise MessageError(404, "Booking not found")
    if booking.user_id is None:
        raise MessageError(400, "Messaging isn't available for a guest booking")

    receiver_id = _assert_is_party(booking, caller_user_id, db)

    msg = Message(
        booking_id=booking_id,
        sender_id=caller_user_id,
        receiver_id=receiver_id,
        content=content.strip(),
        created_at=datetime.now(timezone.utc),
        is_read=False,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)

    sender = db.query(User).filter(User.user_id == caller_user_id).first()

    receiver = db.query(User).filter(User.user_id == receiver_id).first()
    if receiver:
        try:
            from app.utils.notifications import send_push_to_user
            sender_name = f"{sender.f_name} {sender.l_name}" if sender else "Someone"
            send_push_to_user(
                receiver,
                f"New message from {sender_name}",
                content.strip()[:100],
                {"booking_id": booking_id, "type": "message"},
                db=db,
            )
        except Exception:
            pass

    return _message_dict(msg, sender)


def get_conversation(
    *, booking_id: str, caller_user_id: str, limit: int = 50, offset: int = 0, db: Session
) -> dict:
    """Return messages for a booking, oldest first. Marks received messages as read."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise MessageError(404, "Booking not found")

    _assert_is_party(booking, caller_user_id, db)

    query = (
        db.query(Message)
        .filter(Message.booking_id == booking_id)
        .order_by(Message.created_at.asc())
    )
    total = query.count()
    messages = query.offset(offset).limit(limit).all()

    # Mark unread messages sent to the caller as read
    unread_ids = [m.message_id for m in messages if not m.is_read and m.receiver_id == caller_user_id]
    if unread_ids:
        db.query(Message).filter(Message.message_id.in_(unread_ids)).update(
            {"is_read": True}, synchronize_session=False
        )
        db.commit()

    sender_ids = {m.sender_id for m in messages}
    senders = {u.user_id: u for u in db.query(User).filter(User.user_id.in_(sender_ids)).all()}

    return {
        "items": [_message_dict(m, senders.get(m.sender_id)) for m in messages],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def mark_read(*, message_id: str, caller_user_id: str, db: Session) -> dict:
    """Mark a single message as read. Caller must be the receiver."""
    msg = db.query(Message).filter(Message.message_id == message_id).first()
    if not msg:
        raise MessageError(404, "Message not found")
    if msg.receiver_id != caller_user_id:
        raise MessageError(403, "You can only mark your own received messages as read")
    msg.is_read = True
    db.commit()
    return {"message_id": msg.message_id, "is_read": True}


def get_unread_count(*, caller_user_id: str, db: Session) -> dict:
    """Return the total number of unread messages for the current user."""
    count = (
        db.query(Message)
        .filter(Message.receiver_id == caller_user_id, Message.is_read == False)
        .count()
    )
    return {"unread_count": count}
