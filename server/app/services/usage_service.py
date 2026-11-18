"""Usage events (DECISIONS #57): counting how often vendors use what's built
for them, so version 2 of Contract Builder from Chat, and any AI after it,
is judged on real use rather than guessed.

The web app reports a few named actions. Each name has its own allowed
fields, each a short string or a small number, so nothing a person typed —
no message text, no names, no addresses — can be stored here by accident or
on purpose. What the backend can count itself (inquiry requests sent,
inquiries that followed) is counted from its own rows instead.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import GroupMessage, Lead, UsageEvent

# name -> {field: allowed values, or None for a small non-negative integer}
ALLOWED: dict[str, dict[str, set[str] | None]] = {
    # The vendor opened the Mentioned in chat panel; how many it listed.
    "mention_panel_opened": {"count": None},
    # The vendor clicked Use on a mention, and which kind it was.
    "mention_used": {"kind": {"date", "times", "start", "guests", "hours", "amount", "addon", "discount"}},
    # The vendor sent the inquiry form: from Messages or from the editor.
    "inquiry_form_sent": {"from": {"messages", "editor"}},
    # The vendor sent or copied their inquiry link from a lead.
    "inquiry_link_shared": {"how": {"email", "copy"}},
}

# A ceiling per person per day, so a bug in a page can't fill the table.
DAILY_CAP = 500


class UsageError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clean(name: str, props: dict | None) -> dict:
    allowed = ALLOWED.get(name)
    if allowed is None:
        raise UsageError(422, f"Unknown usage event: {name}")
    out: dict = {}
    for key, value in (props or {}).items():
        if key not in allowed:
            raise UsageError(422, f"{name} doesn't take {key}")
        values = allowed[key]
        if values is None:
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 10_000:
                raise UsageError(422, f"{key} must be a small whole number")
        elif value not in values:
            raise UsageError(422, f"{key} must be one of {', '.join(sorted(values))}")
        out[key] = value
    return out


def record(*, name: str, props: dict | None, caller_user_id: str, db: Session) -> dict:
    props = _clean(name, props)
    today = _now() - timedelta(days=1)
    recent = db.query(func.count(UsageEvent.id)).filter(
        UsageEvent.user_id == caller_user_id, UsageEvent.created_at >= today,
    ).scalar()
    if recent >= DAILY_CAP:
        # Counting is best effort: a capped event is dropped, not an error the page has to handle.
        return {"recorded": False}
    db.add(UsageEvent(name=name, user_id=caller_user_id, props=props or None, created_at=_now()))
    db.commit()
    return {"recorded": True}


def summary(*, days: int, db: Session) -> dict:
    """Counts over the last `days`: each event by name (and Use by kind), plus
    inquiry requests sent and how many of those threads then got an inquiry."""
    since = _now() - timedelta(days=days)
    events = db.query(UsageEvent).filter(UsageEvent.created_at >= since).all()
    by_name: dict[str, int] = {}
    used_by_kind: dict[str, int] = {}
    vendors = set()
    for e in events:
        by_name[e.name] = by_name.get(e.name, 0) + 1
        vendors.add(e.user_id)
        if e.name == "mention_used":
            kind = (e.props or {}).get("kind", "unknown")
            used_by_kind[kind] = used_by_kind.get(kind, 0) + 1

    requests = db.query(GroupMessage.conversation_id, func.min(GroupMessage.created_at)).filter(
        GroupMessage.kind == "inquiry_request", GroupMessage.created_at >= since,
    ).group_by(GroupMessage.conversation_id).all()
    asked = {cid: at for cid, at in requests}
    answered = 0
    if asked:
        leads = db.query(Lead.conversation_id, Lead.updated_at).filter(
            Lead.conversation_id.in_(list(asked)), Lead.source == "inquiry_form",
        ).all()
        answered = len({cid for cid, at in leads if at and at >= asked[cid]})
    return {
        "days": days,
        "events": by_name,
        "mention_used_by_kind": used_by_kind,
        "people": len(vendors),
        "inquiry_requests": {"threads_asked": len(asked), "threads_answered": answered},
    }
