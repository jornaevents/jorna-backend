"""A vendor asking for the inquiry form in a chat, and has_inquiry (DECISIONS
#54): every contract starts from the client's inquiry, so a chat that began
with a question gets the form sent into it, and the thread says whether the
client has sent one."""

from datetime import datetime, timedelta, timezone

from app.db.models import GroupMessage
from tests.test_api import TestingSessionLocal, client
from tests.test_public_inquiries import (  # noqa: F401 (fixtures)
    _client,
    _isolate,
    _send,
    _vendor,
    emails,
)


def _ask_question(vendor: dict, who: dict) -> str:
    resp = client.post(
        "/conversations/enquiry",
        json={"vendor_id": vendor["vendor_id"], "content": "Do you travel to Edison?"},
        headers=who["headers"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["conversation_id"]


def _thread(conversation_id: str, who: dict) -> dict:
    resp = client.get(f"/conversations/{conversation_id}", headers=who["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


def _request(conversation_id: str, who: dict):
    return client.post(f"/conversations/{conversation_id}/inquiry-request", headers=who["headers"])


def test_a_question_thread_has_no_inquiry_and_the_vendor_can_send_the_form():
    v, c = _vendor(), _client()
    conv = _ask_question(v, c)
    assert _thread(conv, v)["has_inquiry"] is False
    assert _thread(conv, c)["has_inquiry"] is False

    resp = _request(conv, v)
    assert resp.status_code == 201, resp.text
    message = resp.json()
    assert message["kind"] == "inquiry_request"
    assert message["meta"] == {"vendor_id": v["vendor_id"]}
    # Readable where the card isn't drawn (iOS): who, what, and where to go.
    assert message["content"].startswith("Arjun Kapoor asked for your event details.")
    assert f"/conversation/?id={conv}" in message["content"]

    seen = client.get(f"/conversations/{conv}/messages", headers=c["headers"]).json()
    items = seen["items"]
    assert any(m["kind"] == "inquiry_request" for m in items)


def test_the_list_carries_has_inquiry_too():
    v, c = _vendor(), _client()
    conv = _ask_question(v, c)
    row = next(r for r in client.get("/conversations", headers=v["headers"]).json() if r["conversation_id"] == conv)
    assert row["has_inquiry"] is False


def test_once_a_day_at_most():
    v, c = _vendor(), _client()
    conv = _ask_question(v, c)
    assert _request(conv, v).status_code == 201
    again = _request(conv, v)
    assert again.status_code == 429
    assert again.json()["detail"] == "You asked Priya today. You can ask again tomorrow."

    # A day later it's allowed again.
    db = TestingSessionLocal()
    try:
        db.query(GroupMessage).filter(
            GroupMessage.conversation_id == conv, GroupMessage.kind == "inquiry_request",
        ).update({"created_at": datetime.now(timezone.utc) - timedelta(hours=25)})
        db.commit()
    finally:
        db.close()
    assert _request(conv, v).status_code == 201


def test_the_inquiry_lands_in_the_same_thread_and_then_counts(emails):  # noqa: F811
    v, c = _vendor(), _client()
    conv = _ask_question(v, c)
    assert _request(conv, v).status_code == 201

    sent = _send(v["vendor_id"], c)
    assert sent.status_code == 201, sent.text
    assert sent.json()["conversation_id"] == conv

    assert _thread(conv, v)["has_inquiry"] is True
    assert _thread(conv, c)["has_inquiry"] is True
    done = _request(conv, v)
    assert done.status_code == 409
    assert done.json()["detail"] == "Priya has already sent their event details"


def test_a_lead_the_vendor_added_doesnt_count_until_the_client_fills_in_the_form(emails):  # noqa: F811
    v, c = _vendor(), _client()
    conv = _ask_question(v, c)
    added = client.post(f"/conversations/{conv}/lead", headers=v["headers"])
    assert added.status_code in (200, 201), added.text
    assert _thread(conv, v)["has_inquiry"] is False
    assert _request(conv, v).status_code == 201

    assert _send(v["vendor_id"], c).status_code == 201
    assert _thread(conv, v)["has_inquiry"] is True


def test_only_the_vendor_in_the_chat_can_ask():
    v, c, other = _vendor(), _client(), _vendor()
    conv = _ask_question(v, c)
    assert _request(conv, c).status_code == 403
    assert _request(conv, other).status_code == 403
