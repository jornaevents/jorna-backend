"""Usage events (DECISIONS #57): named actions with allowed fields only, a
daily ceiling, and an admin summary that also counts inquiry requests and
the inquiries that followed."""

import uuid

from app.db.models import UsageEvent, User
from app.services import usage_service
from tests.test_api import TestingSessionLocal, client, make_auth_headers
from tests.test_public_inquiries import (  # noqa: F401 (fixtures)
    _client,
    _isolate,
    _send,
    _vendor,
    emails,
)


def _person(admin: bool = False) -> dict:
    uid = uuid.uuid4().hex[:8]
    db = TestingSessionLocal()
    user = User(email=f"use_{uid}@test.com", username=f"use_{uid}", password="pw", f_name="A", l_name="B",
                token_version=0, is_admin=admin)
    db.add(user)
    db.commit()
    db.refresh(user)
    out = {"user_id": user.user_id, "headers": make_auth_headers(user)}
    db.close()
    return out


def _post(who: dict, name: str, props: dict | None = None):
    return client.post("/usage-events", json={"name": name, "props": props}, headers=who["headers"])


def _rows(user_id: str) -> list[UsageEvent]:
    db = TestingSessionLocal()
    try:
        return db.query(UsageEvent).filter(UsageEvent.user_id == user_id).all()
    finally:
        db.close()


def test_a_known_event_is_recorded_with_its_fields():
    p = _person()
    resp = _post(p, "mention_used", {"kind": "guests"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"recorded": True}
    [row] = _rows(p["user_id"])
    assert (row.name, row.props) == ("mention_used", {"kind": "guests"})


def test_unknown_names_fields_and_values_are_refused():
    p = _person()
    assert _post(p, "anything").status_code == 422
    assert _post(p, "mention_used", {"kind": "guests", "quote": "180 guests please"}).status_code == 422
    assert _post(p, "mention_used", {"kind": "a whole message"}).status_code == 422
    assert _post(p, "mention_panel_opened", {"count": "12"}).status_code == 422
    assert _post(p, "mention_panel_opened", {"count": -1}).status_code == 422
    assert _rows(p["user_id"]) == []


def test_signed_out_is_refused():
    assert client.post("/usage-events", json={"name": "mention_used", "props": {"kind": "date"}}).status_code == 401


def test_over_the_daily_ceiling_is_dropped_not_an_error(monkeypatch):
    monkeypatch.setattr(usage_service, "DAILY_CAP", 2)
    p = _person()
    assert _post(p, "inquiry_form_sent", {"from": "messages"}).json() == {"recorded": True}
    assert _post(p, "inquiry_form_sent", {"from": "editor"}).json() == {"recorded": True}
    third = _post(p, "inquiry_form_sent", {"from": "editor"})
    assert third.status_code == 200 and third.json() == {"recorded": False}
    assert len(_rows(p["user_id"])) == 2


def test_the_summary_is_for_admins_and_counts_requests_answered(emails):  # noqa: F811
    p, admin = _person(), _person(admin=True)
    _post(p, "mention_panel_opened", {"count": 3})
    _post(p, "mention_used", {"kind": "date"})
    _post(p, "mention_used", {"kind": "date"})

    v, c = _vendor(), _client()
    conv = client.post(
        "/conversations/enquiry", json={"vendor_id": v["vendor_id"], "content": "Hi!"}, headers=c["headers"],
    ).json()["conversation_id"]
    assert client.post(f"/conversations/{conv}/inquiry-request", headers=v["headers"]).status_code == 201
    assert _send(v["vendor_id"], c).status_code == 201

    assert client.get("/admin/usage-summary", headers=p["headers"]).status_code == 403
    resp = client.get("/admin/usage-summary?days=7", headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["events"]["mention_used"] >= 2
    assert body["mention_used_by_kind"]["date"] >= 2
    assert body["inquiry_requests"]["threads_asked"] >= 1
    assert body["inquiry_requests"]["threads_answered"] >= 1
