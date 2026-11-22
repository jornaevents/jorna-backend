"""Propose a detail in chat (DECISIONS #59): either side proposes a date, the
times, a guest count or an add-on; the other agrees or declines; a newer
proposal for the same field replaces the open one."""

from datetime import date, timedelta

from app.db.models import GroupMessage, Service
from tests.test_api import TestingSessionLocal, client
from tests.test_public_inquiries import (  # noqa: F401 (fixtures)
    _client,
    _isolate,
    _send,
    _vendor,
)


def _chat(v: dict, c: dict) -> str:
    resp = client.post(
        "/conversations/enquiry", json={"vendor_id": v["vendor_id"], "content": "Hi!"}, headers=c["headers"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["conversation_id"]


def _propose(who: dict, conv: str, field: str, value, **extra):
    body = {"field": field, "value": value, **extra}
    return client.post(f"/conversations/{conv}/details", json=body, headers=who["headers"])


def _answer(who: dict, conv: str, message_id: str, how: str):
    return client.post(f"/conversations/{conv}/details/{message_id}/{how}", headers=who["headers"])


def _future(days=200) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def test_the_client_proposes_times_and_the_vendor_agrees():
    v, c = _vendor(), _client()
    conv = _chat(v, c)
    resp = _propose(c, conv, "times", "16:00-22:30")
    assert resp.status_code == 201, resp.text
    msg = resp.json()
    assert msg["kind"] == "detail_proposal"
    assert msg["meta"]["field"] == "times" and msg["meta"]["value"] == "16:00-22:30"
    assert msg["meta"]["label"] == "4:00 PM – 10:30 PM" and msg["meta"]["status"] == "open"
    assert msg["content"].startswith("Priya proposed the times: 4:00 PM – 10:30 PM.")

    agreed = _answer(v, conv, msg["message_id"], "agree")
    assert agreed.status_code == 200, agreed.text
    assert agreed.json()["meta"]["status"] == "agreed"
    assert agreed.json()["meta"]["answered_by"] == v["user_id"]


def test_the_vendor_proposes_and_the_client_declines():
    v, c = _vendor(), _client()
    conv = _chat(v, c)
    msg = _propose(v, conv, "guests", 150).json()
    assert msg["meta"]["label"] == "150 guests"
    assert msg["content"].startswith("Arjun Kapoor proposed a guest count: 150 guests.")
    declined = _answer(c, conv, msg["message_id"], "decline")
    assert declined.json()["meta"]["status"] == "declined"
    # Answered once only.
    assert _answer(c, conv, msg["message_id"], "agree").status_code == 409


def test_nobody_agrees_with_their_own_proposal():
    v, c = _vendor(), _client()
    conv = _chat(v, c)
    msg = _propose(c, conv, "date", _future()).json()
    assert _answer(c, conv, msg["message_id"], "agree").status_code == 403


def test_a_newer_proposal_for_the_same_field_replaces_the_open_one():
    v, c = _vendor(), _client()
    conv = _chat(v, c)
    first = _propose(c, conv, "guests", 200).json()
    second = _propose(v, conv, "guests", 150).json()
    other = _propose(c, conv, "date", _future()).json()

    db = TestingSessionLocal()
    try:
        status = {m.message_id: m.meta["status"] for m in db.query(GroupMessage).filter(GroupMessage.kind == "detail_proposal", GroupMessage.conversation_id == conv)}
    finally:
        db.close()
    assert status[first["message_id"]] == "replaced"
    assert status[second["message_id"]] == "open"
    assert status[other["message_id"]] == "open"
    assert _answer(v, conv, first["message_id"], "agree").status_code == 409


def test_values_are_checked_as_the_contract_checks_them():
    v, c = _vendor(), _client()
    conv = _chat(v, c)
    assert _propose(c, conv, "date", "2020-01-01").status_code == 422
    assert _propose(c, conv, "date", "next friday").status_code == 422
    assert _propose(c, conv, "times", "4pm-10pm").status_code == 422
    assert _propose(c, conv, "guests", 0).status_code == 422
    assert _propose(c, conv, "price", 0).status_code == 422
    assert _propose(c, conv, "price", "lots").status_code == 422
    assert _propose(c, conv, "price", "2400", unit="week").status_code == 422
    assert _propose(c, conv, "guests", 100, unit="hour").status_code == 422
    assert _propose(c, conv, "venue", "x").status_code == 422
    assert _propose(c, conv, "addon", "nope").status_code == 422


def test_an_add_on_must_be_one_of_the_vendors():
    v, c = _vendor(), _client()
    db = TestingSessionLocal()
    try:
        service = db.query(Service).filter(Service.vendor_id == v["vendor_id"]).first()
        service.add_ons = [{"id": "ao-drone", "name": "Drone coverage", "price": 400, "price_unit": "event"}]
        db.commit()
        service_id = service.service_id
    finally:
        db.close()
    conv = _chat(v, c)
    msg = _propose(c, conv, "addon", "ao-drone").json()
    assert msg["meta"]["label"] == "Drone coverage"
    assert msg["meta"]["addon"] == {"id": "ao-drone", "name": "Drone coverage", "price": 400, "price_unit": "event", "service_id": service_id}


def test_only_the_two_sides_of_the_thread():
    v, c, stranger = _vendor(), _client(), _client()
    conv = _chat(v, c)
    assert _propose(stranger, conv, "guests", 100).status_code == 403


def _add_package(vendor_id: str, name: str, unit: str) -> str:
    db = TestingSessionLocal()
    try:
        row = Service(
            name=name, price=150.0, price_unit=unit, vendor_id=vendor_id, experience="e",
            category="photography", negotiable=False, status="active",
        )
        db.add(row)
        db.commit()
        return row.service_id
    finally:
        db.close()


def test_a_price_takes_the_unit_of_the_vendors_only_package():
    v, c = _vendor(), _client()
    conv = _chat(v, c)
    msg = _propose(v, conv, "price", "$2,400").json()
    assert msg["meta"]["value"] == "2400" and msg["meta"]["unit"] == "event"
    assert msg["meta"]["package"]["name"] == "Sangeet coverage"
    assert msg["meta"]["label"] == "$2,400 per event for Sangeet coverage"
    assert msg["content"].startswith("Arjun Kapoor proposed a price: $2,400 per event for Sangeet coverage.")
    assert _answer(c, conv, msg["message_id"], "agree").json()["meta"]["status"] == "agreed"


def test_a_price_takes_the_unit_of_the_package_on_the_inquiry():
    v, c = _vendor(), _client()
    hourly = _add_package(v["vendor_id"], "Dhol hourly", "hour")
    resp = _send(v["vendor_id"], c, service_id=hourly)
    assert resp.status_code in (200, 201), resp.text
    conv = resp.json()["conversation_id"]
    msg = _propose(c, conv, "price", "175.5").json()
    assert msg["meta"]["value"] == "175.50" and msg["meta"]["unit"] == "hour"
    assert msg["meta"]["package"] == {"service_id": hourly, "name": "Dhol hourly"}
    assert msg["meta"]["label"] == "$175.50 per hour for Dhol hourly"


def test_a_given_unit_wins_and_no_package_means_per_event():
    v, c = _vendor(), _client()
    _add_package(v["vendor_id"], "Dhol hourly", "hour")
    conv = _chat(v, c)
    # Two packages and no inquiry: no package to tell from.
    msg = _propose(c, conv, "price", 3000).json()
    assert msg["meta"]["unit"] == "event" and "package" not in msg["meta"]
    assert msg["meta"]["label"] == "$3,000 per event"
    msg = _propose(c, conv, "price", 40, unit="person").json()
    assert msg["meta"]["unit"] == "person" and msg["meta"]["label"] == "$40 per person"
