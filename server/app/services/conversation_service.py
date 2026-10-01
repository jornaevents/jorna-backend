"""Business logic for conversations.

A conversation is about one of three things, and `subject_type` says which:

    bundle    the group chat for a plan — every vendor on it, and the client
    booking   the client and one vendor, privately, about one booking
    enquiry   the client and one vendor, before anything is booked

Only the first existed. The second was a separate table with its own service and
no caller anywhere in the web app; the third could not exist at all, because a
conversation needed a bundle. See migration 0042.
"""

import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import (
    Booking, Bundle, Conversation, ConversationMember,
    GroupMessage, GroupMessageRead, Service, User, UserBlock, Vendor,
)

logger = logging.getLogger(__name__)

SUBJECT_BUNDLE = "bundle"
SUBJECT_BOOKING = "booking"
SUBJECT_ENQUIRY = "enquiry"

# ── Limits ────────────────────────────────────────────────────────────
#
# Item 8 in CLIENT_FLOW_PLAN deferred client→vendor messaging with a condition:
# build it *with* the limits, because it is the one surface a spammer can reach
# every vendor through. These are those limits.
#
# slowapi is wired (app/limiter.py) but keyed on IP at 60/minute, which is flood
# control, not spam control — one client on one connection can be well inside it
# and still contact three hundred vendors. These are per user and enforced here,
# where the rule is about who is asking rather than where from.

# The anti-spray rule, and the one that matters. A client may hold this many
# enquiries that no vendor has answered yet; the next one is refused until
# somebody replies. Bulk contact can only take this shape, and a client with a
# real question has one or two outstanding, not ten.
#
# Note this is not the "no second thread to a vendor who hasn't replied" from
# MESSAGING_PROPOSAL: threads are one per client↔vendor pair, so a second thread
# to the same vendor never happens — it reuses the first. Written per-pair, the
# same intent is a cap across vendors.
MAX_UNANSWERED_ENQUIRIES = 3

# A ceiling for the pathological case, where every vendor answers promptly and
# the rule above never bites.
MAX_ENQUIRIES_PER_DAY = 10

# Flood control, per user rather than per IP, across every thread they're in.
MAX_MESSAGES_PER_MINUTE = 20


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


def _display_name(conv: Conversation, members: list[User], caller_user_id: str, db: Session) -> str:
    """What to call this chat, from the caller's side of it.

    A bundle chat is named for the plan and keeps the name it was given. The
    two-person threads are not: "Priya Sharma" is the right name for a thread
    the vendor is reading and the wrong one for the client, who is not talking
    to themselves. So they are named at read time, per viewer, and their stored
    `name` is empty.
    """
    if conv.subject_type == SUBJECT_BUNDLE:
        return conv.name

    other = next((u for u in members if u.user_id != caller_user_id), None)
    who = f"{other.f_name} {other.l_name}".strip() if other else "Conversation"

    if conv.subject_type == SUBJECT_BOOKING and conv.booking_id:
        booking = db.query(Booking).filter(Booking.booking_id == conv.booking_id).first()
        service = (
            db.query(Service).filter(Service.service_id == booking.service_id).first()
            if booking else None
        )
        # The service, not the plan: this thread is about one booking, and the
        # plan's own chat is a separate row in the same list.
        return f"{who} · {service.name}" if service else who

    return who


def _conversation_dict(
    conv: Conversation,
    members: list[User],
    last_message: GroupMessage | None,
    *,
    caller_user_id: str | None = None,
    unread_count: int = 0,
    db: Session | None = None,
) -> dict:
    return {
        "conversation_id": conv.conversation_id,
        "subject_type": conv.subject_type,
        "bundle_id": conv.bundle_id,
        "booking_id": conv.booking_id,
        "vendor_id": conv.vendor_id,
        "type": conv.type,
        "name": (
            _display_name(conv, members, caller_user_id, db)
            if caller_user_id and db is not None
            else conv.name
        ),
        "member_count": len(members),
        "members": [_member_dict(u) for u in members],
        # Per row, not one number for the whole inbox. The tab could say "you
        # have 4 unread" and not which of eleven chats they were in.
        "unread_count": unread_count,
        "last_message": {
            "content": last_message.content,
            "sender_id": last_message.sender_id,
            "kind": last_message.kind,
            "created_at": last_message.created_at.isoformat(),
        } if last_message else None,
        "created_at": conv.created_at.isoformat(),
    }


def _unread_by_conversation(
    conv_ids: list[str], caller_user_id: str, db: Session
) -> dict[str, int]:
    """Unread counts for many conversations in one query.

    A per-conversation count inside the list loop would be one query per row on
    a page that already does several; this is the same arithmetic done once.
    """
    if not conv_ids:
        return {}
    rows = (
        db.query(GroupMessage.conversation_id, func.count(GroupMessage.message_id))
        .outerjoin(
            GroupMessageRead,
            (GroupMessageRead.message_id == GroupMessage.message_id)
            & (GroupMessageRead.user_id == caller_user_id),
        )
        .filter(
            GroupMessage.conversation_id.in_(conv_ids),
            GroupMessageRead.read_id.is_(None),
        )
        .group_by(GroupMessage.conversation_id)
        .all()
    )
    counts = {conv_id: count for conv_id, count in rows}
    # "Mark as unread": a thread the reader flagged counts as unread even with
    # every message read, until they open it again.
    flagged = db.query(ConversationMember.conversation_id).filter(
        ConversationMember.conversation_id.in_(conv_ids),
        ConversationMember.user_id == caller_user_id,
        ConversationMember.marked_unread_at.isnot(None),
    ).all()
    for (conv_id,) in flagged:
        counts[conv_id] = max(counts.get(conv_id, 0), 1)
    return counts


def _message_dict(msg: GroupMessage, sender: User | None, read_by: list[str]) -> dict:
    return {
        "message_id": msg.message_id,
        "conversation_id": msg.conversation_id,
        "sender_id": msg.sender_id,
        "sender_name": f"{sender.f_name} {sender.l_name}" if sender else None,
        "sender_pfp": sender.pfp_url if sender else None,
        "content": msg.content,
        "created_at": msg.created_at.isoformat(),
        "kind": msg.kind,
        "meta": msg.meta,
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
            subject_type=SUBJECT_BUNDLE,
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
        from app.utils.notifications import send_push_to_user
        for uid in vendor_user_ids:
            user = db.query(User).filter(User.user_id == uid).first()
            if user:
                send_push_to_user(
                    user,
                    "You've been added to a group chat",
                    f"You're now part of group chats for bundle '{bundle.name}'.",
                    {"bundle_id": bundle_id, "type": "added_to_conversation"},
                    db=db,
                )
    except Exception as exc:
        logger.warning("Failed to notify vendors of group chat creation: %s", exc)

    return created


# ── Two-person threads ────────────────────────────────────────────────


def _vendor_user(vendor_id: str, db: Session) -> User:
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise ConversationError(404, "Vendor not found")
    user = db.query(User).filter(User.user_id == vendor.user_id).first()
    if not user:
        raise ConversationError(404, "Vendor has no account")
    return user


def _assert_not_blocked(a: str, b: str, db: Session) -> None:
    """Refuse in either direction.

    UserBlock's own docstring says it is "enforced client-side", which is not
    enforcement — it is a request that the other end behave. It costs one query
    to mean it, and this is the surface where it matters: everywhere else a
    block hides content, here it would be a stranger arriving in an inbox.
    """
    blocked = db.query(UserBlock).filter(
        ((UserBlock.blocker_user_id == a) & (UserBlock.blocked_user_id == b))
        | ((UserBlock.blocker_user_id == b) & (UserBlock.blocked_user_id == a))
    ).first()
    if blocked:
        raise ConversationError(403, "You can't message this person.")


def _create_thread(
    *, subject_type: str, client_user_id: str, other_user_id: str,
    vendor_id: str | None, booking_id: str | None, db: Session,
    bundle_id: str | None = None,
) -> Conversation:
    now = datetime.now(timezone.utc)
    conv = Conversation(
        subject_type=subject_type,
        bundle_id=bundle_id,
        booking_id=booking_id,
        vendor_id=vendor_id,
        client_user_id=client_user_id,
        type="direct",
        # Named per viewer at read time — see _display_name.
        name="",
        created_at=now,
    )
    db.add(conv)
    db.flush()
    for uid in dict.fromkeys([client_user_id, other_user_id]):
        db.add(ConversationMember(
            conversation_id=conv.conversation_id, user_id=uid, joined_at=now,
        ))
    db.commit()
    db.refresh(conv)
    return conv


def _assert_enquiry_allowed(client_user_id: str, db: Session) -> None:
    """The limits from MESSAGING_PROPOSAL §Limits, checked on opening a thread.

    Deliberately not checked on every message: a conversation a vendor is taking
    part in is not spam, and a client who has to watch a counter while talking
    to someone who answered them is being punished for the behaviour we wanted.
    """
    unanswered = 0
    threads = db.query(Conversation).filter(
        Conversation.subject_type == SUBJECT_ENQUIRY,
        Conversation.client_user_id == client_user_id,
    ).all()
    for t in threads:
        replied = db.query(GroupMessage).filter(
            GroupMessage.conversation_id == t.conversation_id,
            GroupMessage.sender_id != client_user_id,
        ).first()
        if not replied:
            unanswered += 1
    if unanswered >= MAX_UNANSWERED_ENQUIRIES:
        raise ConversationError(
            429,
            f"You have {unanswered} questions still waiting for an answer. "
            "Give them a chance to reply before starting another.",
        )

    since = datetime.now(timezone.utc) - timedelta(days=1)
    today = db.query(Conversation).filter(
        Conversation.subject_type == SUBJECT_ENQUIRY,
        Conversation.client_user_id == client_user_id,
        Conversation.created_at >= since,
    ).count()
    if today >= MAX_ENQUIRIES_PER_DAY:
        raise ConversationError(429, "That's a lot of vendors for one day. Try again tomorrow.")


def open_enquiry(
    *, vendor_id: str, content: str, caller_user_id: str, service_id: str | None = None,
    db: Session,
) -> dict:
    """Open — or reuse — the thread between this client and this vendor.

    One thread per pair, not per listing. A client comparing three of a vendor's
    packages is one customer with one question, and three near-identical threads
    would have the vendor answering the same person three times. The listing it
    was asked from rides along on the message as a reference card, so the
    subject is carried without being the thread's identity.
    """
    if not content.strip():
        raise ConversationError(400, "Message content cannot be empty")

    vendor_user = _vendor_user(vendor_id, db)
    if vendor_user.user_id == caller_user_id:
        raise ConversationError(400, "You can't message yourself.")
    _assert_not_blocked(caller_user_id, vendor_user.user_id, db)

    existing = db.query(Conversation).filter(
        Conversation.subject_type == SUBJECT_ENQUIRY,
        Conversation.vendor_id == vendor_id,
        Conversation.client_user_id == caller_user_id,
    ).first()

    if existing:
        conv = existing
    else:
        _assert_enquiry_allowed(caller_user_id, db)
        conv = _create_thread(
            subject_type=SUBJECT_ENQUIRY,
            client_user_id=caller_user_id,
            other_user_id=vendor_user.user_id,
            vendor_id=vendor_id,
            booking_id=None,
            db=db,
        )

    meta = None
    if service_id:
        service = db.query(Service).filter(Service.service_id == service_id).first()
        if service and service.vendor_id == vendor_id:
            meta = {"service_id": service.service_id, "service_name": service.name}

    message = send_group_message(
        conversation_id=conv.conversation_id,
        content=content,
        caller_user_id=caller_user_id,
        db=db,
        meta=meta,
    )
    return {"conversation_id": conv.conversation_id, "message": message}


def open_booking_thread(*, booking_id: str, caller_user_id: str, db: Session) -> dict:
    """The private thread for one booking. Either party may open it.

    The plan's group chat has every vendor on it, so it is the wrong room for
    "can you arrive an hour earlier?" — a question about one booking, asked in
    front of five other businesses. Idempotent: opening is how you get to it.
    """
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        raise ConversationError(404, "Booking not found")
    if booking.user_id is None:
        raise ConversationError(400, "Messaging isn't available for a guest booking")
    vendor_user = _vendor_user(booking.vendor_id, db)

    if caller_user_id not in (booking.user_id, vendor_user.user_id):
        raise ConversationError(403, "You are not a party to this booking")
    _assert_not_blocked(booking.user_id, vendor_user.user_id, db)

    conv = db.query(Conversation).filter(
        Conversation.subject_type == SUBJECT_BOOKING,
        Conversation.booking_id == booking_id,
    ).first()
    if not conv:
        conv = _create_thread(
            subject_type=SUBJECT_BOOKING,
            client_user_id=booking.user_id,
            other_user_id=vendor_user.user_id,
            vendor_id=booking.vendor_id,
            booking_id=booking_id,
            bundle_id=booking.bundle_id,
            db=db,
        )
    elif conv.bundle_id is None and booking.bundle_id is not None:
        # Self-heal a thread created before bundle_id was tracked here — every
        # open is a chance to backfill it, on top of (not instead of) the
        # one-time migration for threads nobody happens to open again.
        conv.bundle_id = booking.bundle_id
        db.commit()
        db.refresh(conv)

    return get_conversation(
        conversation_id=conv.conversation_id, caller_user_id=caller_user_id, db=db
    )


def post_offer_message(
    *, booking_id: str, sender_user_id: str, content: str, meta: dict, db: Session
) -> None:
    """Write a negotiation event into the booking's thread.

    Best-effort by design: a price offer that saved and then failed to post a
    message would be an offer the other party never heard about, and the offer
    is the part that matters. Called by negotiation_service, which owns the
    money; this owns only the sentence about it.
    """
    try:
        booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
        if not booking:
            return
        thread = open_booking_thread(
            booking_id=booking_id, caller_user_id=sender_user_id, db=db
        )
        send_group_message(
            conversation_id=thread["conversation_id"],
            content=content,
            caller_user_id=sender_user_id,
            db=db,
            kind="offer",
            meta=meta,
            skip_limits=True,
        )
    except Exception as exc:
        logger.warning("Couldn't post offer message for booking %s: %s", booking_id, exc)


def post_system_message(
    *, booking_id: str, sender_user_id: str, content: str, meta: dict | None, db: Session
) -> None:
    """Write an event-log line into the booking's thread — a reschedule
    proposed, accepted, declined, withdrawn, or expired.

    Same best-effort shape as post_offer_message and the same reasoning:
    change_request_service owns the reschedule itself, this owns only the
    sentence about it, and that sentence failing to post must never fail
    the reschedule action that triggered it. `sender_user_id` attributes
    the line (whoever's action caused it) but nothing renders it as coming
    from them — the frontend shows kind="system" as an unattributed,
    centered line, not a chat bubble.
    """
    try:
        booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
        if not booking:
            return
        thread = open_booking_thread(
            booking_id=booking_id, caller_user_id=sender_user_id, db=db
        )
        send_group_message(
            conversation_id=thread["conversation_id"],
            content=content,
            caller_user_id=sender_user_id,
            db=db,
            kind="system",
            meta=meta,
            skip_limits=True,
        )
    except Exception as exc:
        logger.warning("Couldn't post system message for booking %s: %s", booking_id, exc)


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
        from app.utils.notifications import send_push_to_user
        bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
        user = db.query(User).filter(User.user_id == vendor_user_id).first()
        if user and bundle:
            send_push_to_user(
                user,
                "You've been added to a group chat",
                f"You're now part of group chats for bundle '{bundle.name}'.",
                {"bundle_id": bundle_id, "type": "added_to_conversation"},
                db=db,
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


def _subject_is_live(conv: Conversation, live_bundles: set[str], live_bookings: set[str]) -> bool:
    """Is the thing this conversation is about still there?

    A chat outliving its subject is an orphan, and orphans should never
    resurface in an inbox. This used to be one line — keep it only if its bundle
    is live — which was right when every conversation had a bundle and became a
    silent delete of everything else the moment they didn't: an enquiry would be
    created, sent, notified, and never appear in anyone's list.
    """
    if conv.subject_type == SUBJECT_BOOKING:
        return conv.booking_id in live_bookings
    if conv.subject_type == SUBJECT_ENQUIRY:
        # Nothing to outlive. An enquiry is about a vendor, and a vendor leaving
        # takes their user with them, which takes the membership row.
        return True
    return conv.bundle_id in live_bundles


def list_conversations(*, caller_user_id: str, db: Session) -> list[dict]:
    """Return every conversation the caller is in, newest activity first."""
    memberships = db.query(ConversationMember).filter(
        ConversationMember.user_id == caller_user_id
    ).all()
    conv_ids = [m.conversation_id for m in memberships]
    conversations = db.query(Conversation).filter(
        Conversation.conversation_id.in_(conv_ids)
    ).all()

    bundle_ids = {c.bundle_id for c in conversations if c.bundle_id}
    booking_ids = {c.booking_id for c in conversations if c.booking_id}
    live_bundles = {
        bid for (bid,) in db.query(Bundle.bundle_id).filter(
            Bundle.bundle_id.in_(bundle_ids)
        ).all()
    } if bundle_ids else set()
    live_bookings = {
        bid for (bid,) in db.query(Booking.booking_id).filter(
            Booking.booking_id.in_(booking_ids)
        ).all()
    } if booking_ids else set()
    conversations = [
        c for c in conversations if _subject_is_live(c, live_bundles, live_bookings)
    ]

    unread = _unread_by_conversation([c.conversation_id for c in conversations], caller_user_id, db)

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
        result.append(_conversation_dict(
            conv, members, last_msg,
            caller_user_id=caller_user_id,
            unread_count=unread.get(conv.conversation_id, 0),
            db=db,
        ))

    # Ordered by what happened last, not by when the chat was made. Created-at
    # order puts a plan you opened in January above the vendor who answered you
    # this morning, and an inbox sorted by anything other than activity is one
    # people scroll rather than read.
    result.sort(
        key=lambda c: (c["last_message"] or {}).get("created_at") or c["created_at"],
        reverse=True,
    )
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
    unread = _unread_by_conversation([conversation_id], caller_user_id, db)
    return _conversation_dict(
        conv, members, last_msg,
        caller_user_id=caller_user_id,
        unread_count=unread.get(conversation_id, 0),
        db=db,
    )


# ── Messaging ─────────────────────────────────────────────────────────


def _assert_not_flooding(caller_user_id: str, db: Session) -> None:
    """Per-user flood control, across every thread they're in.

    slowapi's default is per IP, which a phone on a shared network shares with
    strangers and a script on one connection sits comfortably inside. Counted
    from the messages themselves so it holds across replicas, which an
    in-process counter would not.
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    recent = db.query(GroupMessage).filter(
        GroupMessage.sender_id == caller_user_id,
        GroupMessage.created_at >= since,
    ).count()
    if recent >= MAX_MESSAGES_PER_MINUTE:
        raise ConversationError(429, "You're sending messages too quickly. Wait a moment.")


def send_group_message(
    *, conversation_id: str, content: str, caller_user_id: str, db: Session,
    kind: str = "text", meta: dict | None = None, skip_limits: bool = False,
) -> dict:
    """Send a message to a conversation.

    `kind` is what the message *is* — text, an offer, a system notice. `content`
    is a human sentence whatever the kind, so a client that doesn't know one
    still renders something true; the kind only decides whether a card is drawn
    around it.

    `skip_limits` is for messages the server writes on someone's behalf — an
    offer posted by negotiation_service is the user's action, but it is rate
    limited where that action is taken, and counting it twice would let a
    haggle exhaust a client's own allowance.
    """
    if not content.strip():
        raise ConversationError(400, "Message content cannot be empty")
    _assert_is_member(conversation_id, caller_user_id, db)
    if not skip_limits:
        _assert_not_flooding(caller_user_id, db)

    msg = GroupMessage(
        conversation_id=conversation_id,
        sender_id=caller_user_id,
        content=content.strip(),
        created_at=datetime.now(timezone.utc),
        kind=kind,
        meta=meta,
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
        from app.utils.notifications import send_push_to_user
        for member in members:
            user = db.query(User).filter(User.user_id == member.user_id).first()
            if user:
                send_push_to_user(
                    user,
                    f"New message from {sender_name}",
                    content.strip()[:100],
                    {"conversation_id": conversation_id, "type": "group_message"},
                    db=db,
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
    if offset == 0:
        # Opening the thread is reading it, so a "mark as unread" ends here.
        # Paging back through history (offset > 0) isn't.
        db.query(ConversationMember).filter(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == caller_user_id,
            ConversationMember.marked_unread_at.isnot(None),
        ).update({ConversationMember.marked_unread_at: None}, synchronize_session=False)
        db.commit()
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
    # A flagged thread with nothing actually unread still counts once — the
    # badge and the thread list must agree (see _unread_by_conversation).
    for m in memberships:
        if m.marked_unread_at is None:
            continue
        thread_msgs = {
            r.message_id for r in db.query(GroupMessage.message_id)
            .filter(GroupMessage.conversation_id == m.conversation_id).all()
        }
        if not (thread_msgs - read_msg_ids):
            unread += 1
    return {"unread_count": unread}


def mark_unread(*, conversation_id: str, caller_user_id: str, db: Session) -> dict:
    """Flag a thread as unread for the caller only, until they next open it."""
    member = db.query(ConversationMember).filter(
        ConversationMember.conversation_id == conversation_id,
        ConversationMember.user_id == caller_user_id,
    ).first()
    if not member:
        raise ConversationError(404, "Conversation not found")
    member.marked_unread_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    return get_conversation(conversation_id=conversation_id, caller_user_id=caller_user_id, db=db)
