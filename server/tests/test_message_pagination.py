"""Message pagination returns the newest window, not the oldest.

Regression for the bug where a conversation with more than `limit` messages only
ever loaded its oldest `limit` — hiding every newer message on open/reload.
"""
import uuid
from datetime import datetime, timedelta, timezone

from app.db.models import GroupMessage
from tests.test_api import TestingSessionLocal, client, make_auth_headers
from tests.test_conversations import seeded_db, _create_bundle  # noqa: F401


def _seed_messages(conversation_id: str, sender_id: str, count: int):
    db = TestingSessionLocal()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(count):
        db.add(GroupMessage(
            message_id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            sender_id=sender_id,
            content=f"msg-{i:03d}",
            created_at=base + timedelta(minutes=i),
        ))
    db.commit()
    db.close()


def test_first_page_returns_newest_messages_in_order(seeded_db):
    _bundle_id, conv_ids = _create_bundle(seeded_db)
    conv_id = conv_ids[0]
    member = seeded_db["client_user"]

    _seed_messages(conv_id, member.user_id, 55)

    page = client.get(
        f"/conversations/{conv_id}/messages?limit=50",
        headers=make_auth_headers(member),
    ).json()

    assert page["total"] == 55
    assert len(page["items"]) == 50
    contents = [m["content"] for m in page["items"]]
    # Newest window (msg-005 … msg-054), chronological within the page.
    assert contents[0] == "msg-005"
    assert contents[-1] == "msg-054"
    assert contents == sorted(contents)  # oldest → newest


def test_offset_pages_back_into_history(seeded_db):
    _bundle_id, conv_ids = _create_bundle(seeded_db)
    conv_id = conv_ids[0]
    member = seeded_db["client_user"]

    _seed_messages(conv_id, member.user_id, 55)

    older = client.get(
        f"/conversations/{conv_id}/messages?limit=50&offset=50",
        headers=make_auth_headers(member),
    ).json()

    # The remaining oldest five (msg-000 … msg-004).
    contents = [m["content"] for m in older["items"]]
    assert contents == ["msg-000", "msg-001", "msg-002", "msg-003", "msg-004"]
