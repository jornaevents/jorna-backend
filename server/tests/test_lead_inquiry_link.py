"""A vendor emailing a lead they typed in their inquiry form link (DECISIONS
#56), and the inquiry that follows landing on that same lead."""

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Lead
from tests.test_api import TestingSessionLocal, client
from tests.test_public_inquiries import (  # noqa: F401 (fixtures)
    _client,
    _isolate,
    _leads,
    _send,
    _vendor,
    emails,
)


@pytest.fixture
def link_emails(monkeypatch):
    import app.services.contract_service as cs

    sent = []
    monkeypatch.setattr(cs, "send_email", lambda **kw: sent.append(kw) or {"success": True})
    return sent


def _typed_lead(v: dict, email: str, **extra) -> str:
    resp = client.post("/leads", json={"name": "Meera Patel", "email": email, **extra}, headers=v["headers"])
    assert resp.status_code == 201, resp.text
    return resp.json()["lead_id"]


def _link(v: dict, lead_id: str):
    return client.post(f"/leads/{lead_id}/inquiry-link", headers=v["headers"])


def test_the_lead_is_emailed_the_vendors_inquiry_link(link_emails):
    v = _vendor()
    lead_id = _typed_lead(v, "meera@example.com")

    resp = _link(v, lead_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sent"] is True and body["sent_to"] == "meera@example.com"
    assert body["link"].endswith(f"/inquire/?vendor={v['vendor_id']}")

    [mail] = link_emails
    assert mail["to"] == "meera@example.com"
    assert mail["subject"] == "Arjun Kapoor sent you their inquiry form"
    assert body["link"] in mail["html"] and "Hi Meera," in mail["html"]
    assert mail["reply_to"] == v["email"]


def test_once_a_day_at_most(link_emails):
    v = _vendor()
    lead_id = _typed_lead(v, "meera@example.com")
    assert _link(v, lead_id).status_code == 200
    again = _link(v, lead_id)
    assert again.status_code == 429
    assert again.json()["detail"] == "You sent them your inquiry link today. You can send it again tomorrow."

    db = TestingSessionLocal()
    try:
        db.query(Lead).filter(Lead.lead_id == lead_id).update(
            {Lead.inquiry_link_sent_at: datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=25)}
        )
        db.commit()
    finally:
        db.close()
    assert _link(v, lead_id).status_code == 200


def test_it_needs_an_email_and_an_open_lead(link_emails):
    v = _vendor()
    no_email = client.post("/leads", json={"name": "Meera Patel"}, headers=v["headers"]).json()["lead_id"]
    assert _link(v, no_email).status_code == 400

    lost = _typed_lead(v, "lost@example.com")
    assert client.patch(f"/leads/{lost}", json={"status": "lost"}, headers=v["headers"]).status_code == 200
    assert _link(v, lost).status_code == 400
    assert link_emails == []


def test_only_the_leads_vendor(link_emails):
    v, other = _vendor(), _vendor()
    lead_id = _typed_lead(v, "meera@example.com")
    assert _link(other, lead_id).status_code in (403, 404)


def test_a_failed_email_says_so_and_can_be_retried(monkeypatch):
    import app.services.contract_service as cs

    monkeypatch.setattr(cs, "send_email", lambda **kw: {"success": False})
    v = _vendor()
    lead_id = _typed_lead(v, "meera@example.com")
    resp = _link(v, lead_id)
    assert resp.status_code == 502
    # Not counted against the day's one.
    monkeypatch.setattr(cs, "send_email", lambda **kw: {"success": True})
    assert _link(v, lead_id).status_code == 200


def test_the_inquiry_lands_on_the_lead_it_was_sent_to(link_emails, emails):  # noqa: F811
    v, c = _vendor(), _client()
    lead_id = _typed_lead(v, c["email"].upper())
    assert _link(v, lead_id).status_code == 200

    sent = _send(v["vendor_id"], c)
    assert sent.status_code == 201, sent.text

    [lead] = _leads(v["vendor_id"])
    assert lead.lead_id == lead_id
    assert (lead.source, lead.user_id, lead.conversation_id) == ("inquiry_form", c["user_id"], sent.json()["conversation_id"])
    assert lead.event_type == "Sangeet"

    # And the chat now has its inquiry, so the link is for Messages now.
    assert _link(v, lead_id).status_code == 409


def test_another_clients_inquiry_doesnt_take_the_lead(link_emails, emails):  # noqa: F811
    v, c = _vendor(), _client()
    lead_id = _typed_lead(v, "someone-else@example.com")
    assert _send(v["vendor_id"], c).status_code == 201
    leads = _leads(v["vendor_id"])
    assert len(leads) == 2
    assert next(lead for lead in leads if lead.lead_id == lead_id).user_id is None
