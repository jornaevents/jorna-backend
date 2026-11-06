"""The inquiry form, POST /vendors/{id}/inquiries (docs/DECISIONS.md #32,
#45): a signed-in client with a confirmed email, never a vendor. It opens
their thread with the vendor, lands in Leads, emails both sides, and keeps
the abuse guards."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.db.models import (
    Conversation,
    ConversationMember,
    GroupMessage,
    GroupMessageRead,
    Lead,
    Service,
    User,
    UserBlock,
    Vendor,
)
from app.limiter import limiter
from tests.test_api import TestingSessionLocal, client, make_auth_headers

_created = {"users": [], "vendors": []}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _isolate():
    """No rate limit except where a test turns it on (the suite shares one
    client address), and everything made here is removed afterwards."""
    limiter.enabled = False
    yield
    limiter.enabled = True
    db = TestingSessionLocal()
    if _created["vendors"]:
        convs = [
            c.conversation_id
            for c in db.query(Conversation).filter(Conversation.vendor_id.in_(_created["vendors"])).all()
        ]
        if convs:
            msgs = [m.message_id for m in db.query(GroupMessage).filter(GroupMessage.conversation_id.in_(convs)).all()]
            if msgs:
                db.query(GroupMessageRead).filter(GroupMessageRead.message_id.in_(msgs)).delete(synchronize_session=False)
            db.query(GroupMessage).filter(GroupMessage.conversation_id.in_(convs)).delete(synchronize_session=False)
            db.query(ConversationMember).filter(ConversationMember.conversation_id.in_(convs)).delete(synchronize_session=False)
            db.query(Conversation).filter(Conversation.conversation_id.in_(convs)).delete(synchronize_session=False)
        db.query(Lead).filter(Lead.vendor_id.in_(_created["vendors"])).delete(synchronize_session=False)
        db.query(Service).filter(Service.vendor_id.in_(_created["vendors"])).delete(synchronize_session=False)
        db.query(Vendor).filter(Vendor.vendor_id.in_(_created["vendors"])).delete(synchronize_session=False)
    if _created["users"]:
        db.query(UserBlock).filter(UserBlock.blocker_user_id.in_(_created["users"])).delete(synchronize_session=False)
        db.query(User).filter(User.user_id.in_(_created["users"])).delete(synchronize_session=False)
    db.commit()
    db.close()
    for v in _created.values():
        v.clear()


@pytest.fixture
def emails():
    with patch("app.services.inquiry_service.send_email", return_value={"success": True, "id": "e1"}) as sent:
        yield sent


def _user(prefix: str, *, f_name="Priya", l_name="Shah", verified=True, phone=None) -> User:
    uid = uuid.uuid4().hex[:8]
    db = TestingSessionLocal()
    user = User(
        email=f"{prefix}_{uid}@test.com", username=f"{prefix}_{uid}", password="pw", phone=phone,
        f_name=f_name, l_name=l_name, token_version=0, email_verified_at=_now() if verified else None,
    )
    db.add(user); db.commit(); db.refresh(user)
    db.expunge(user)
    db.close()
    _created["users"].append(user.user_id)
    return user


def _vendor(package_status: str | None = "active") -> dict:
    user = _user("inq_v", f_name="Arjun", l_name="Kapoor")
    db = TestingSessionLocal()
    vendor = Vendor(user_id=user.user_id, bio="b", category="photography", rating=0.0, num_events=0, payment_method="manual")
    db.add(vendor); db.commit(); db.refresh(vendor)
    if package_status:
        db.add(Service(
            name="Sangeet coverage", price=2500.0, price_unit="event", vendor_id=vendor.vendor_id,
            experience="e", category="photography", negotiable=False, status=package_status,
        ))
        db.commit()
    out = {"vendor_id": vendor.vendor_id, "user_id": user.user_id, "email": user.email, "headers": make_auth_headers(user)}
    _created["vendors"].append(vendor.vendor_id)
    db.close()
    return out


def _client(**kw) -> dict:
    user = _user("inq_c", **kw)
    return {"user_id": user.user_id, "email": user.email, "headers": make_auth_headers(user)}


def _future(days=200) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()


def _send(vendor_id: str, who: dict | None, **overrides):
    body = {
        "phone": "(732) 555-0100", "event_date_iso": _future(), "event_type": "Sangeet",
        "guest_count": 180, "note": "Looking for someone for our sangeet in Edison.",
    }
    body.update(overrides)
    return client.post(f"/vendors/{vendor_id}/inquiries", json=body, headers=who["headers"] if who else {})


def _leads(vendor_id: str) -> list[Lead]:
    db = TestingSessionLocal()
    try:
        return db.query(Lead).filter(Lead.vendor_id == vendor_id).all()
    finally:
        db.close()


def _thread_messages(conversation_id: str) -> list[GroupMessage]:
    db = TestingSessionLocal()
    try:
        return (
            db.query(GroupMessage).filter(GroupMessage.conversation_id == conversation_id)
            .order_by(GroupMessage.created_at).all()
        )
    finally:
        db.close()


def test_an_inquiry_opens_the_thread_and_lands_in_leads_as_needing_the_vendor(emails):
    v, c = _vendor(), _client()
    resp = _send(v["vendor_id"], c)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["received"] is True and body["vendor_name"] == "Arjun Kapoor"
    assert set(body) == {"received", "vendor_name", "conversation_id"}

    [lead] = _leads(v["vendor_id"])
    assert (lead.source, lead.status, lead.event_type, lead.guest_count) == ("inquiry_form", "new", "Sangeet", 180)
    # Who it's from is the account, not anything typed.
    assert (lead.name, lead.email, lead.user_id) == ("Priya Shah", c["email"], c["user_id"])
    assert lead.conversation_id == body["conversation_id"]
    assert lead.phone == "(732) 555-0100" and lead.note.startswith("Looking for")

    [message] = _thread_messages(body["conversation_id"])
    assert message.sender_id == c["user_id"]
    assert message.content.startswith("Sangeet · ") and "about 180 guests" in message.content
    assert message.content.endswith("Looking for someone for our sangeet in Edison.")

    pipe = client.get("/leads/pipeline", headers=v["headers"]).json()
    item = next(i for i in pipe["items"] if i["id"] == f"lead:{lead.lead_id}")
    assert item["attention"] == "needs_you" and item["attention_reason"] == "new_lead"


def test_signed_out_is_refused():
    v = _vendor()
    assert _send(v["vendor_id"], None).status_code == 401
    assert _leads(v["vendor_id"]) == []


def test_an_unconfirmed_email_is_refused(emails):
    v = _vendor()
    resp = _send(v["vendor_id"], _client(verified=False))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Confirm your email before sending an inquiry"
    assert _leads(v["vendor_id"]) == [] and emails.call_count == 0


def test_a_vendor_account_is_refused(emails):
    v, other = _vendor(), _vendor()
    resp = _send(v["vendor_id"], other)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Switch to a client account to send inquiries"
    # Themselves too.
    assert _send(v["vendor_id"], v).status_code == 403
    assert _leads(v["vendor_id"]) == []


def test_typed_name_and_email_from_an_older_page_are_ignored(emails):
    v, c = _vendor(), _client()
    resp = _send(v["vendor_id"], c, name="Someone Else", email="else@example.com")
    assert resp.status_code == 201, resp.text
    [lead] = _leads(v["vendor_id"])
    assert (lead.name, lead.email) == ("Priya Shah", c["email"])


def test_the_vendor_is_emailed_and_the_client_gets_a_copy_of_what_they_sent(emails):
    v, c = _vendor(), _client()
    conv = _send(v["vendor_id"], c, note="<script>x</script> Call me after 6").json()["conversation_id"]
    to_vendor, to_client = (call.kwargs for call in emails.call_args_list)

    assert to_vendor["to"] == v["email"]
    assert to_vendor["reply_to"] == c["email"]
    assert to_vendor["subject"] == "New inquiry from Priya Shah"
    assert f"/conversation/?id={conv}" in to_vendor["html"]
    assert "&lt;script&gt;" in to_vendor["html"] and "<script>" not in to_vendor["html"]

    assert to_client["to"] == c["email"]
    assert to_client["subject"] == "Your inquiry to Arjun Kapoor"
    assert "Call me after 6" in to_client["html"] and "Sangeet" in to_client["html"]
    assert "&lt;script&gt;" in to_client["html"] and "<script>" not in to_client["html"]
    assert f"/client/conversation/?id={conv}" in to_client["html"]


def test_asking_again_reuses_the_thread_and_the_lead_and_confirms_once_a_day(emails):
    v, c = _vendor(), _client()
    first = _send(v["vendor_id"], c).json()["conversation_id"]
    db = TestingSessionLocal()
    db.query(Lead).filter(Lead.vendor_id == v["vendor_id"]).update({Lead.status: "contacted"})
    db.commit(); db.close()

    second = _send(v["vendor_id"], c, guest_count=220, note="One more thing", event_type=None).json()["conversation_id"]

    assert first == second
    assert len(_thread_messages(first)) == 2
    [lead] = _leads(v["vendor_id"])
    assert (lead.status, lead.guest_count, lead.event_type, lead.note) == ("new", 220, "Sangeet", "One more thing")
    recipients = [call.kwargs["to"] for call in emails.call_args_list]
    assert recipients == [v["email"], c["email"], v["email"]]


def test_an_inquiry_after_a_question_joins_that_thread(emails):
    v, c = _vendor(), _client()
    asked = client.post("/conversations/enquiry", json={"vendor_id": v["vendor_id"], "content": "Hi!"}, headers=c["headers"])
    assert asked.status_code == 201, asked.text
    conv = _send(v["vendor_id"], c).json()["conversation_id"]
    assert conv == asked.json()["conversation_id"]


def test_a_blocked_client_cant_send_one(emails):
    v, c = _vendor(), _client()
    db = TestingSessionLocal()
    db.add(UserBlock(blocker_user_id=v["user_id"], blocked_user_id=c["user_id"], created_at=_now()))
    db.commit(); db.close()
    resp = _send(v["vendor_id"], c)
    assert resp.status_code == 403, resp.text
    assert _leads(v["vendor_id"]) == [] and emails.call_count == 0


def test_unknown_vendors_and_vendors_still_in_setup_are_not_found(emails):
    c = _client()
    assert _send(str(uuid.uuid4()), c).status_code == 404
    assert _send(_vendor(package_status=None)["vendor_id"], c).status_code == 404
    assert _send(_vendor(package_status="archived")["vendor_id"], c).status_code == 404
    # A hidden package still counts: the vendor is set up, just selective.
    assert _send(_vendor(package_status="hidden")["vendor_id"], c).status_code == 201
    assert emails.call_count == 2


@pytest.mark.parametrize("field,value,message", [
    ("phone", "12", "phone number doesn't look right"),
    ("event_date_iso", "2020-01-01", "already passed"),
    ("event_date_iso", "next fall", "YYYY-MM-DD"),
    ("guest_count", 0, "between 1 and 100,000"),
    ("guest_count", 200_000, "between 1 and 100,000"),
])
def test_each_field_is_checked(emails, field, value, message):
    v = _vendor()
    resp = _send(v["vendor_id"], _client(), **{field: value})
    assert resp.status_code == 422, resp.text
    assert message in resp.json()["detail"]
    assert _leads(v["vendor_id"]) == [] and emails.call_count == 0


def test_optional_fields_can_be_left_out(emails):
    v, c = _vendor(), _client(phone="(201) 555-0199")
    resp = client.post(f"/vendors/{v['vendor_id']}/inquiries", json={}, headers=c["headers"])
    assert resp.status_code == 201, resp.text
    [lead] = _leads(v["vendor_id"])
    assert (lead.event_date_iso, lead.guest_count, lead.event_type) == (None, None, None)
    # The account's phone when none is typed.
    assert lead.phone == "(201) 555-0199"
    assert "Not sure yet" in emails.call_args_list[0].kwargs["html"]
    [message] = _thread_messages(resp.json()["conversation_id"])
    assert message.content == "Sent you an inquiry from your Jorna page."


def test_a_filled_honeypot_gets_the_normal_reply_and_nothing_else(emails):
    v = _vendor()
    resp = _send(v["vendor_id"], _client(), website="http://spam.example")
    assert resp.status_code == 201
    assert resp.json() == {"received": True, "vendor_name": "Arjun Kapoor", "conversation_id": None}
    assert _leads(v["vendor_id"]) == [] and emails.call_count == 0


def test_a_vendor_takes_at_most_fifty_inquiries_a_day(emails):
    v = _vendor()
    db = TestingSessionLocal()
    now = _now()
    for n in range(50):
        db.add(Lead(vendor_id=v["vendor_id"], name=f"L{n}", email=f"l{n}@example.com", status="new",
                    source="inquiry_form", created_at=now - timedelta(hours=1), updated_at=now))
    # Older ones, and ones the vendor added, don't count.
    db.add(Lead(vendor_id=v["vendor_id"], name="Old", status="new", source="inquiry_form",
                created_at=now - timedelta(days=2), updated_at=now))
    db.commit(); db.close()

    resp = _send(v["vendor_id"], _client())
    assert resp.status_code == 429
    assert "try again tomorrow" in resp.json()["detail"]
    assert emails.call_count == 0


def test_a_client_with_three_unanswered_vendors_waits(emails):
    c = _client()
    for _ in range(3):
        assert _send(_vendor()["vendor_id"], c).status_code == 201
    resp = _send(_vendor()["vendor_id"], c)
    assert resp.status_code == 429
    assert "still waiting for an answer" in resp.json()["detail"]


def test_five_a_minute_from_one_address():
    v = _vendor()
    limiter.reset()
    limiter.enabled = True
    with patch("app.services.inquiry_service.send_email", return_value={"success": True}):
        codes = [_send(v["vendor_id"], _client()).status_code for _ in range(6)]
    assert codes == [201] * 5 + [429]


def test_vendor_added_leads_take_the_new_fields_too():
    v = _vendor()
    made = client.post("/leads", json={"name": "Iyer Family", "event_type": "Wedding", "guest_count": 250}, headers=v["headers"])
    assert made.status_code == 201, made.text
    assert (made.json()["source"], made.json()["event_type"], made.json()["guest_count"]) == ("manual", "Wedding", 250)

    lead_id = made.json()["lead_id"]
    changed = client.patch(f"/leads/{lead_id}", json={"guest_count": 300}, headers=v["headers"])
    assert changed.json()["guest_count"] == 300
    bad = client.patch(f"/leads/{lead_id}", json={"guest_count": -4}, headers=v["headers"])
    assert bad.status_code == 422


def _package_id(vendor_id: str) -> str:
    db = TestingSessionLocal()
    try:
        return db.query(Service).filter(Service.vendor_id == vendor_id).first().service_id
    finally:
        db.close()


def test_location_and_package_land_on_the_lead_the_thread_and_the_emails(emails):
    v, c = _vendor(), _client()
    package = _package_id(v["vendor_id"])
    resp = _send(v["vendor_id"], c, event_location="  Edison, NJ ", service_id=package)
    assert resp.status_code == 201, resp.text

    [lead] = _leads(v["vendor_id"])
    assert (lead.event_location, lead.service_id) == ("Edison, NJ", package)

    [message] = _thread_messages(resp.json()["conversation_id"])
    assert "Edison, NJ" in message.content and "Sangeet coverage" in message.content
    assert message.meta and message.meta.get("service_id") == package

    to_vendor, to_client = (call.kwargs for call in emails.call_args_list)
    assert "Edison, NJ" in to_vendor["html"] and "Sangeet coverage" in to_vendor["html"]
    assert "Edison, NJ" in to_client["html"]

    item = next(
        i for i in client.get("/leads/pipeline", headers=v["headers"]).json()["items"]
        if i["id"] == f"lead:{lead.lead_id}"
    )
    assert (item["location"], item["service_name"], item["service_id"]) == ("Edison, NJ", "Sangeet coverage", package)
    listed = client.get("/leads", headers=v["headers"]).json()["items"][0]
    assert (listed["event_location"], listed["service_id"], listed["service_name"]) == (
        "Edison, NJ", package, "Sangeet coverage",
    )


def test_no_package_is_not_sure_yet(emails):
    v = _vendor()
    resp = _send(v["vendor_id"], _client())
    assert resp.status_code == 201
    [lead] = _leads(v["vendor_id"])
    assert (lead.service_id, lead.event_location) == (None, None)
    assert "Not sure yet" in emails.call_args_list[0].kwargs["html"]


def test_someone_elses_or_an_archived_package_is_refused(emails):
    v, other = _vendor(), _vendor()
    resp = _send(v["vendor_id"], _client(), service_id=_package_id(other["vendor_id"]))
    assert resp.status_code == 422
    assert "package isn't available" in resp.json()["detail"]

    db = TestingSessionLocal()
    db.query(Service).filter(Service.vendor_id == v["vendor_id"]).update({Service.status: "archived"})
    db.add(Service(
        name="Haldi", price=900.0, price_unit="event", vendor_id=v["vendor_id"],
        experience="e", category="photography", negotiable=False, status="active",
    ))
    db.commit(); db.close()
    db = TestingSessionLocal()
    old = db.query(Service).filter(Service.vendor_id == v["vendor_id"], Service.status == "archived").first().service_id
    db.close()
    resp = _send(v["vendor_id"], _client(), service_id=old)
    assert resp.status_code == 422
    assert _leads(v["vendor_id"]) == [] and emails.call_count == 0


def test_the_vendor_can_edit_the_location():
    v, c = _vendor(), _client()
    with patch("app.services.inquiry_service.send_email", return_value={"success": True}):
        _send(v["vendor_id"], c, event_location="Edison")
    [lead] = _leads(v["vendor_id"])
    resp = client.patch(f"/leads/{lead.lead_id}", json={"event_location": "Oak Tree Manor, Edison"}, headers=v["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["event_location"] == "Oak Tree Manor, Edison"
