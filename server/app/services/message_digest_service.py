"""The periodic "what you missed" email.

Every new message already tries a push notification at send time
(conversation_service.send_group_message) — this is not a fallback for that.
It's a separate, slower signal: every few minutes, anyone with messages
they still haven't read since their last digest gets one email listing what's
new, across every thread they're in. Batched rather than one email per
message, so an active back-and-forth doesn't fill an inbox with one email
per line.

A message lands in exactly one digest: the next sweep after it arrives,
tracked per user by User.last_message_digest_at. It's excluded from that
digest (and every later one) once read — read in the app before the sweep
runs, and there's nothing left to tell them; read after, and repeating it in
the next digest would just be noise about something they've already seen.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.config import WEB_APP_URL
from app.db.models import ConversationMember, GroupMessage, GroupMessageRead, User
from app.services.email_service import send_email

logger = logging.getLogger(__name__)

# User has no created_at column, so a user who has never been digested (Null
# last_message_digest_at) needs some cutoff to start from. Anything before
# the app existed works — this just means someone's first-ever digest covers
# their whole unread backlog once, rather than silently starting empty and
# only catching messages sent after this feature shipped.
_NEVER_DIGESTED = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _conversation_url(conversation_id: str) -> str:
    return f"{WEB_APP_URL}/conversation/?id={conversation_id}"


def _inbox_url() -> str:
    return f"{WEB_APP_URL}/messages/"


def _unread_for_user(user_id: str, since: datetime, db: Session) -> list[GroupMessage]:
    """Every message still unread by this user, in a thread they're on, sent
    since `since`. Sender's own messages never appear: send_group_message
    marks a message read for its own sender the moment it's created."""
    conv_ids = [
        m.conversation_id
        for m in db.query(ConversationMember).filter(ConversationMember.user_id == user_id).all()
    ]
    if not conv_ids:
        return []
    return (
        db.query(GroupMessage)
        .outerjoin(
            GroupMessageRead,
            (GroupMessageRead.message_id == GroupMessage.message_id)
            & (GroupMessageRead.user_id == user_id),
        )
        .filter(
            GroupMessage.conversation_id.in_(conv_ids),
            GroupMessage.created_at > since,
            GroupMessageRead.read_id.is_(None),
        )
        .order_by(GroupMessage.created_at.asc())
        .all()
    )


def _conversation_rows(user_id: str, messages: list[GroupMessage], db: Session) -> list[dict]:
    """One row per conversation with new messages, named the way that
    conversation is named for this user — reuses get_conversation rather than
    re-deriving a display name, so a digest and the inbox never disagree
    about what to call a thread."""
    from app.services.conversation_service import get_conversation

    by_conv: dict[str, int] = {}
    order: list[str] = []
    for m in messages:
        if m.conversation_id not in by_conv:
            order.append(m.conversation_id)
            by_conv[m.conversation_id] = 0
        by_conv[m.conversation_id] += 1

    rows = []
    for conv_id in order:
        try:
            conv = get_conversation(conversation_id=conv_id, caller_user_id=user_id, db=db)
        except Exception:
            continue
        count = by_conv[conv_id]
        rows.append({
            "name": conv.get("name") or "A conversation",
            "count": count,
            "url": _conversation_url(conv_id),
        })
    return rows


def _subject(total: int) -> str:
    if total == 1:
        return "You have a new message on Jorna"
    return f"You have {total} new messages on Jorna"


def _body(*, name: str, rows: list[dict], total: int) -> tuple[str, str]:
    greeting = f"Hi {name}," if name else "Hi,"
    line = "a new message" if total == 1 else f"{total} new messages"

    items_html = "".join(
        f"""
        <tr>
          <td style="padding:6px 0;color:#35101b">
            <a href="{r['url']}" style="color:#6b1226;text-decoration:none;font-weight:600">{r['name']}</a>
          </td>
          <td style="padding:6px 0;color:#6b5c50;text-align:right">
            {r['count']} new
          </td>
        </tr>
        """
        for r in rows
    )
    html = f"""
      <div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:480px;color:#35101b">
        <p>{greeting}</p>
        <p>You have {line} waiting on Jorna:</p>
        <table style="width:100%;border-collapse:collapse;margin:16px 0">
          {items_html}
        </table>
        <p style="margin:28px 0">
          <a href="{_inbox_url()}"
             style="background:#6b1226;color:#f6eee1;text-decoration:none;padding:14px 28px;border-radius:999px;font-weight:600;display:inline-block">
            Open Messages
          </a>
        </p>
      </div>
    """.strip()

    items_text = "\n".join(f"- {r['name']}: {r['count']} new — {r['url']}" for r in rows)
    text = (
        f"{greeting}\n\n"
        f"You have {line} waiting on Jorna:\n\n"
        f"{items_text}\n\n"
        f"Open Messages: {_inbox_url()}\n"
    )
    return html, text


def send_message_digest(user: User, db: Session, *, now: Optional[datetime] = None) -> bool:
    """One user's digest. Marks last_message_digest_at either way — same
    reasoning as send_checkin_reminder: a sweep this frequent should not
    retry a bad address every few minutes, and the window is short enough
    that a missed digest just rolls into the next one."""
    now = now or datetime.now(timezone.utc)
    since = user.last_message_digest_at or _NEVER_DIGESTED
    unread = _unread_for_user(user.user_id, since, db)
    sent = False
    if unread and user.email:
        rows = _conversation_rows(user.user_id, unread, db)
        total = sum(r["count"] for r in rows)
        if total:
            name = (user.f_name or "").strip()
            html, text = _body(name=name, rows=rows, total=total)
            result = send_email(to=user.email, subject=_subject(total), html=html, text=text)
            sent = bool(result.get("success"))
            if not sent:
                logger.info("Message digest for %s not sent: %s", user.user_id, result.get("error"))
    user.last_message_digest_at = now
    db.commit()
    return sent


def send_due_digests(*, db: Session, now: Optional[datetime] = None) -> int:
    """One sweep. Returns how many digest emails went out."""
    now = now or datetime.now(timezone.utc)
    sent = 0
    member_user_ids = {
        row[0] for row in db.query(ConversationMember.user_id).distinct().all()
    }
    for user_id in member_user_ids:
        user = db.query(User).filter(User.user_id == user_id).first()
        if not user:
            continue
        try:
            if send_message_digest(user, db, now=now):
                sent += 1
        except Exception as exc:  # one bad user must not stop the rest
            logger.warning("Message digest for %s failed: %s", user_id, exc)
            db.rollback()
    if sent:
        logger.info("Sent %d message digests", sent)
    return sent
