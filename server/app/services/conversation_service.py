"""Business logic for bundle group conversations."""

import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.db.models import (
    Booking, Bundle, Conversation, ConversationMember,
    GroupMessage, GroupMessageRead, User, Vendor,
)

logger = logging.getLogger(__name__)


class ConversationError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ── Helpers ───────────────────────────────────────────────────────────


def _assert_is_member(conversation_id: str, caller_user_id: str, db: Session) -> None:
    member = db.query(ConversationMember).filter(
        ConversationMember.conversation_id == conversation_id,
        ConversationMember.user_id == caller_user_id,
    ).first()
    if not member:
        raise ConversationError(403, "You are not a member of this conversation")


def _get_vendor_user_ids(bundle_id: str, db: Session) -> list[str]:
    """Return the user_ids of all vendor users in a bundle's bookings."""
    bookings = db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    vendor_user_ids = []
    seen = set()
    for booking in bookings:
        vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
        if vendor and vendor.user_id not in seen:
            vendor_user_ids.append(vendor.user_id)
            seen.add(vendor.user_id)
    return vendor_user_ids


def _member_dict(user: User) -> dict:
    return {
        "user_id": user.user_id,
        "f_name": user.f_name,
        "l_name": user.l_name,
        "pfp_url": user.pfp_url,
    }


def _conversation_dict(conv: Conversation, members: list[User], last_message: GroupMessage | None) -> dict:
    return {
        "conversation_id": conv.conversation_id,
        "bundle_id": conv.bundle_id,
        "type": conv.type,
        "name": conv.name,
        "member_count": len(members),
        "members": [_member_dict(u) for u in members],
        "last_message": {
            "content": last_message.content,
            "sender_id": last_message.sender_id,
            "created_at": last_message.created_at.isoformat(),
        } if last_message else None,
        "created_at": conv.created_at.isoformat(),
    }


def _message_dict(msg: GroupMessage, sender: User | None, read_by: list[str]) -> dict:
    return {
        "message_id": msg.message_id,
        "conversation_id": msg.conversation_id,
        "sender_id": msg.sender_id,
        "sender_name": f"{sender.f_name} {sender.l_name}" if sender else None,
        "sender_pfp": sender.pfp_url if sender else None,
        "content": msg.content,
        "created_at": msg.created_at.isoformat(),
        "read_by": read_by,
    }


# ── Core conversation creation ─────────────────────────────────────────


def create_bundle_conversations(*, bundle_id: str, client_user_id: str, db: Session) -> list[dict]:
    """Create vendors_only and all_parties group chats for a bundle.
    Called automatically when a bundle is created.
    """
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise ConversationError(404, "Bundle not found")

    vendor_user_ids = _get_vendor_user_ids(bundle_id, db)
    now = datetime.now(timezone.utc)
    created = []

    for conv_type, member_ids in [
        ("vendors_only", vendor_user_ids),
        ("all_parties", list(dict.fromkeys([client_user_id] + vendor_user_ids))),
    ]:
        # Skip vendors_only if there are no vendors yet
        if conv_type == "vendors_only" and not vendor_user_ids:
            continue
        label = "Vendors" if conv_type == "vendors_only" else "All Parties"
        conv = Conversation(
            bundle_id=bundle_id,
            type=conv_type,
            name=f"{bundle.name} — {label}",
            created_at=now,
        )
        db.add(conv)
        db.flush()

        for uid in member_ids:
            db.add(ConversationMember(
                conversation_id=conv.conversation_id,
                user_id=uid,
                joined_at=now,
            ))

        db.commit()
        members = db.query(User).filter(User.user_id.in_(member_ids)).all()
        created.append(_conversation_dict(conv, members, None))

    # Notify all vendors that they've been added to group chats
    try:
        from app.utils.notifications import send_push_notification
        for uid in vendor_user_ids:
            user = db.query(User).filter(User.user_id == uid).first()
            if user and user.fcm_token:
                send_push_notification(
                    fcm_token=user.fcm_token,
                    title="You've been added to a group chat",
                    body=f"You're now part of group chats for bundle '{bundle.name}'.",
                    data={"bundle_id": bundle_id, "type": "added_to_conversation"},
                )
    except Exception as exc:
        logger.warning("Failed to notify vendors of group chat creation: %s", exc)

    return created


# ── Membership updates ────────────────────────────────────────────────


def add_vendor_to_bundle_conversations(*, bundle_id: str, vendor_user_id: str, db: Session) -> None:
    """Add a vendor to all conversations for a bundle (called when booking is added)."""
    conversations = db.query(Conversation).filter(Conversation.bundle_id == bundle_id).all()
    now = datetime.now(timezone.utc)
    for conv in conversations:
        existing = db.query(ConversationMember).filter(
            ConversationMember.conversation_id == conv.conversation_id,
            ConversationMember.user_id == vendor_user_id,
        ).first()
        if not existing:
            db.add(ConversationMember(
                conversation_id=conv.conversation_id,
                user_id=vendor_user_id,
                joined_at=now,
            ))
    db.commit()

    # Notify the vendor they've been added
    try:
        from app.utils.notifications import send_push_notification
        bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
        user = db.query(User).filter(User.user_id == vendor_user_id).first()
        if user and user.fcm_token and bundle:
            send_push_notification(
                fcm_token=user.fcm_token,
                title="You've been added to a group chat",
                body=f"You're now part of group chats for bundle '{bundle.name}'.",
                data={"bundle_id": bundle_id, "type": "added_to_conversation"},
            )
    except Exception as exc:
        logger.warning("Failed to notify vendor of conversation addition: %s", exc)


def remove_vendor_from_bundle_conversations(*, bundle_id: str, vendor_user_id: str, db: Session) -> None:
    """Remove a vendor from bundle conversations (called when booking is removed).
    Only removes if the vendor has no other bookings in the bundle.
    """
    remaining = db.query(Booking).filter(
        Booking.bundle_id == bundle_id,
        Booking.vendor_id.in_(
            db.query(Vendor.vendor_id).filter(Vendor.user_id == vendor_user_id)
        ),
    ).count()

    if remaining == 0:
        conversations = db.query(Conversation).filter(Conversation.bundle_id == bundle_id).all()
        for conv in conversations:
            db.query(ConversationMember).filter(
                ConversationMember.conversation_id == conv.conversation_id,
                ConversationMember.user_id == vendor_user_id,
            ).delete()
        db.commit()


# ── Conversation queries ──────────────────────────────────────────────


def list_conversations(*, caller_user_id: str, db: Session) -> list[dict]:
    """Return all conversations the current user is a member of.

    Conversations whose bundle no longer exists are excluded — historical
    deletions (before bundle deletion cascaded to chats) left orphans that
    should never resurface in anyone's inbox.
    """
    memberships = db.query(ConversationMember).filter(
        ConversationMember.user_id == caller_user_id
    ).all()
    conv_ids = [m.conversation_id for m in memberships]
    conversations = db.query(Conversation).filter(
        Conversation.conversation_id.in_(conv_ids)
    ).order_by(Conversation.created_at.desc()).all()

    live_bundle_ids = {
        bid for (bid,) in db.query(Bundle.bundle_id).filter(
            Bundle.bundle_id.in_({c.bundle_id for c in conversations})
        ).all()
    } if conversations else set()
    conversations = [c for c in conversations if c.bundle_id in live_bundle_ids]

    result = []
    for conv in conversations:
        members_rows = db.query(ConversationMember).filter(
            ConversationMember.conversation_id == conv.conversation_id
        ).all()
        user_ids = [m.user_id for m in members_rows]
        members = db.query(User).filter(User.user_id.in_(user_ids)).all()
        last_msg = (
            db.query(GroupMessage)
            .filter(GroupMessage.conversation_id == conv.conversation_id)
            .order_by(GroupMessage.created_at.desc())
            .first()
        )
        result.append(_conversation_dict(conv, members, last_msg))
    return result


def get_conversation(*, conversation_id: str, caller_user_id: str, db: Session) -> dict:
    _assert_is_member(conversation_id, caller_user_id, db)
    conv = db.query(Conversation).filter(Conversation.conversation_id == conversation_id).first()
    if not conv:
        raise ConversationError(404, "Conversation not found")
    members_rows = db.query(ConversationMember).filter(
        ConversationMember.conversation_id == conversation_id
    ).all()
    user_ids = [m.user_id for m in members_rows]
    members = db.query(User).filter(User.user_id.in_(user_ids)).all()
    last_msg = (
        db.query(GroupMessage)
        .filter(GroupMessage.conversation_id == conversation_id)
        .order_by(GroupMessage.created_at.desc())
        .first()
    )
    return _conversation_dict(conv, members, last_msg)


# ── Messaging ─────────────────────────────────────────────────────────


def send_group_message(
    *, conversation_id: str, content: str, caller_user_id: str, db: Session
) -> dict:
    """Send a message to a group conversation."""
    if not content.strip():
        raise ConversationError(400, "Message content cannot be empty")
    _assert_is_member(conversation_id, caller_user_id, db)

    msg = GroupMessage(
        conversation_id=conversation_id,
        sender_id=caller_user_id,
        content=content.strip(),
        created_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)

    # Mark as read for the sender immediately
    db.add(GroupMessageRead(
        message_id=msg.message_id,
        user_id=caller_user_id,
        read_at=msg.created_at,
    ))
    db.commit()

    # Push notify all other members
    members = db.query(ConversationMember).filter(
        ConversationMember.conversation_id == conversation_id,
        ConversationMember.user_id != caller_user_id,
    ).all()
    sender = db.query(User).filter(User.user_id == caller_user_id).first()
    sender_name = f"{sender.f_name} {sender.l_name}" if sender else "Someone"

    try:
        from app.utils.notifications import send_push_notification
        for member in members:
            user = db.query(User).filter(User.user_id == member.user_id).first()
            if user and user.fcm_token:
                send_push_notification(
                    fcm_token=user.fcm_token,
                    title=f"New message from {sender_name}",
                    body=content.strip()[:100],
                    data={"conversation_id": conversation_id, "type": "group_message"},
                )
    except Exception as exc:
        logger.warning("Group message notification failed: %s", exc)

    read_by = [caller_user_id]
    return _message_dict(msg, sender, read_by)


def get_group_messages(
    *, conversation_id: str, caller_user_id: str, limit: int = 50, offset: int = 0, db: Session
) -> dict:
    """Return a page of messages, newest-window first, in chronological order.

    offset=0 returns the most RECENT `limit` messages; higher offsets page further
    back into history. Within the page they're ordered oldest→newest for display.
    (The old ascending+offset paging returned only the OLDEST `limit` messages, so
    once a conversation passed `limit` the newer ones never loaded.) Marks the
    returned messages as read.
    """
    _assert_is_member(conversation_id, caller_user_id, db)

    base = db.query(GroupMessage).filter(
        GroupMessage.conversation_id == conversation_id
    )
    total = base.count()
    messages = (
        base.order_by(GroupMessage.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    messages.reverse()  # oldest → newest for display

    # Mark all fetched messages as read for this user
    now = datetime.now(timezone.utc)
    msg_ids = [m.message_id for m in messages]
    already_read = {
        r.message_id for r in db.query(GroupMessageRead).filter(
            GroupMessageRead.message_id.in_(msg_ids),
            GroupMessageRead.user_id == caller_user_id,
        ).all()
    }
    for msg in messages:
        if msg.message_id not in already_read:
            db.add(GroupMessageRead(
                message_id=msg.message_id,
                user_id=caller_user_id,
                read_at=now,
            ))
    if msg_ids:
        db.commit()

    # Fetch sender info and read receipts
    sender_ids = {m.sender_id for m in messages}
    senders = {u.user_id: u for u in db.query(User).filter(User.user_id.in_(sender_ids)).all()}
    read_receipts = db.query(GroupMessageRead).filter(
        GroupMessageRead.message_id.in_(msg_ids)
    ).all()
    reads_by_message: dict[str, list[str]] = {}
    for r in read_receipts:
        reads_by_message.setdefault(r.message_id, []).append(r.user_id)

    return {
        "items": [
            _message_dict(m, senders.get(m.sender_id), reads_by_message.get(m.message_id, []))
            for m in messages
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_unread_count(*, caller_user_id: str, db: Session) -> dict:
    """Return total unread group messages across all conversations."""
    memberships = db.query(ConversationMember).filter(
        ConversationMember.user_id == caller_user_id
    ).all()
    conv_ids = [m.conversation_id for m in memberships]

    all_msg_ids = [
        m.message_id for m in db.query(GroupMessage.message_id)
        .filter(GroupMessage.conversation_id.in_(conv_ids))
        .all()
    ]
    read_msg_ids = {
        r.message_id for r in db.query(GroupMessageRead).filter(
            GroupMessageRead.message_id.in_(all_msg_ids),
            GroupMessageRead.user_id == caller_user_id,
        ).all()
    }
    unread = len(set(all_msg_ids) - read_msg_ids)
    return {"unread_count": unread}
