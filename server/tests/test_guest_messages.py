"""A client on the contract link messages their vendor (DECISIONS #41).

The link is the whole credential. Without an account their messages land in
the booking's ordinary thread with no sender, the vendor reads and answers in
Messages, and replies reach the client by email — once per stretch of unread
replies. With an account they write as themselves.
"""

import uuid

from app.db.models import Booking, Conversation, GroupMessage, User
from tests.test_api import TestingSessionLocal, client, make_auth_headers
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _create_contract,
    _created,
    _isolate,
    _setup_vendor,
)


def _link_contract(v, **overrides):
    c = _create_contract(v, guest_name="Priya Mehta", guest_email="priya@example.com", **overrides)
    return c, c["contract_token"]


def _vendor_thread(v, booking_id):
    convs = client.get("/conversations", headers=v["headers"]).json()
    return next(c for c in convs if c["booking_id"] == booking_id)


def _cleanup_threads(booking_id):
    db = TestingSessionLocal()
    try:
        ids = [c.conversation_id for c in db.query(Conversation).filter(Conversation.booking_id == booking_id).all()]
        if ids:
            db.query(GroupMessage).filter(GroupMessage.conversation_id.in_(ids)).delete(synchronize_session=False)
    finally:
        db.commit()
        db.close()


def test_a_link_client_writes_and_the_vendor_reads_it_in_messages(mocker):
    mocker.patch("app.services.email_service.send_email", return_value={"success": True, "id": "e1"})
    v = _setup_vendor()
    c, token = _link_contract(v)
    try:
        sent = client.post(f"/guest-bookings/{token}/messages", json={"content": "Could we start at 7:30?"})
        assert sent.status_code == 201, sent.text
        assert sent.json()["items"][-1] == {
            **sent.json()["items"][-1], "from": "you", "sender_name": "Priya Mehta", "content": "Could we start at 7:30?",
        }

        thread = _vendor_thread(v, c["booking_id"])
        assert thread["unread_count"] == 1
        assert thread["name"].startswith("Priya Mehta")
        msgs = client.get(f"/conversations/{thread['conversation_id']}/messages", headers=v["headers"]).json()["items"]
        assert msgs[-1]["sender_id"] is None and msgs[-1]["from_link_client"] is True
        assert msgs[-1]["sender_name"] == "Priya Mehta"
        assert _vendor_thread(v, c["booking_id"])["unread_count"] == 0
    finally:
        _cleanup_threads(c["booking_id"])


def test_replies_email_the_link_client_once_until_they_come_back(mocker):
    sender = mocker.patch("app.services.email_service.send_email", return_value={"success": True, "id": "e1"})
    v = _setup_vendor()
    c, token = _link_contract(v)
    try:
        client.post(f"/guest-bookings/{token}/messages", json={"content": "Hi!"})
        conv_id = _vendor_thread(v, c["booking_id"])["conversation_id"]
        sender.reset_mock()

        reply = lambda text: client.post(
            f"/conversations/{conv_id}/messages", json={"content": text}, headers=v["headers"],
        )
        assert reply("7:30 works.").status_code == 201
        assert reply("I'll update the contract.").status_code == 201
        emails = [k for k in sender.call_args_list if k.kwargs.get("to") == "priya@example.com"]
        assert len(emails) == 1, "one email for the stretch of unread replies"
        assert f"/booking-link?t={token}#messages" in emails[0].kwargs["html"]
        assert emails[0].kwargs["subject"] == "Arjun Kapoor sent you a message"

        seen = client.get(f"/guest-bookings/{token}/messages").json()
        assert [m["from"] for m in seen["items"]] == ["you", "vendor", "vendor"]
        assert seen["vendor_name"] == "Arjun Kapoor"

        reply("Done — check the link.")
        emails = [k for k in sender.call_args_list if k.kwargs.get("to") == "priya@example.com"]
        assert len(emails) == 2, "back on the link, so the next reply emails again"
    finally:
        _cleanup_threads(c["booking_id"])


def test_a_vendor_can_open_the_thread_for_a_link_booking():
    v = _setup_vendor()
    c, _ = _link_contract(v)
    try:
        opened = client.post(f"/conversations/booking/{c['booking_id']}", headers=v["headers"])
        assert opened.status_code == 200, opened.text
        assert opened.json()["name"].startswith("Priya Mehta")
    finally:
        _cleanup_threads(c["booking_id"])


def test_a_client_with_an_account_writes_as_themselves():
    v = _setup_vendor()
    c, token = _link_contract(v)
    db = TestingSessionLocal()
    uid = uuid.uuid4().hex[:8]
    me = User(email=f"gm_c_{uid}@test.com", username=f"gm_c_{uid}", password="pw", f_name="Priya", l_name="Mehta",
              token_version=0)
    db.add(me)
    db.commit()
    db.query(Booking).filter(Booking.booking_id == c["booking_id"]).update({Booking.user_id: me.user_id})
    db.commit()
    me_id = me.user_id
    db.close()
    _created["users"].append(me_id)
    try:
        assert client.post(f"/guest-bookings/{token}/messages", json={"content": "Hello from the link"}).status_code == 201
        convs = client.get("/conversations", headers=make_auth_headers(User(user_id=me_id, email=f"gm_c_{uid}@test.com", token_version=0))).json()
        thread = next(t for t in convs if t["booking_id"] == c["booking_id"])
        assert thread["last_message"]["sender_id"] == me_id
    finally:
        _cleanup_threads(c["booking_id"])


def test_the_link_guards_messages_like_everything_else(mocker):
    mocker.patch("app.services.email_service.send_email", return_value={"success": True, "id": "e1"})
    assert client.get("/guest-bookings/nope/messages").status_code == 404
    v = _setup_vendor()
    c, token = _link_contract(v)
    try:
        assert client.post(f"/guest-bookings/{token}/messages", json={"content": "   "}).status_code == 400
        assert client.post(f"/guest-bookings/{token}/messages", json={"content": "x" * 4001}).status_code == 422
        assert client.post(f"/guest-bookings/{token}/decline", json={}).status_code == 200
        assert client.post(f"/guest-bookings/{token}/messages", json={"content": "Wait"}).status_code == 410
        # Still readable, so they can see what was said.
        assert client.get(f"/guest-bookings/{token}/messages").status_code == 200
    finally:
        _cleanup_threads(c["booking_id"])
