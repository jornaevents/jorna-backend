"""An account holder's contract emails point to their messages, where they
sign; "no account needed" is only for a client who has just the link — a
contract sent before contracts went into the chat (docs/DECISIONS.md #48)."""

from types import SimpleNamespace

from app.db.models import Booking, Vendor
from app.services import document_service, field_negotiation_service, proposal_service
from tests.chat_helpers import as_link_contract, new_chat
from tests.test_api import TestingSessionLocal, client
from tests.test_contract_documents import _clean_documents, _signed_with_schedule  # noqa: F401 — autouse fixture
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _create_contract,
    _isolate,
    _setup_vendor,
)


def _loaded(booking_id: str):
    db = TestingSessionLocal()
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).one()
    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).one()
    return db, booking, vendor


def _reply_html(booking_id: str, monkeypatch) -> str:
    sent: list[dict] = []
    monkeypatch.setattr(proposal_service, "send_email", lambda **kw: sent.append(kw) or {"success": True})
    db, booking, vendor = _loaded(booking_id)
    proposal_service.email_reply(booking, vendor, SimpleNamespace(status="accepted", response_note=None), db)
    db.close()
    return sent[0]["html"]


def test_answering_a_chat_clients_changes_points_to_their_messages(monkeypatch):
    v = _setup_vendor()
    chat = new_chat(v["headers"])
    c = _create_contract(v, conversation_id=chat)
    html = _reply_html(c["booking_id"], monkeypatch)
    assert f"/conversation/?id={chat}" in html
    assert "no account" not in html and "booking-link" not in html


def test_a_link_only_client_keeps_the_link(monkeypatch):
    v = _setup_vendor()
    c = _create_contract(v)
    as_link_contract(c["booking_id"], TestingSessionLocal)
    html = _reply_html(c["booking_id"], monkeypatch)
    assert f"booking-link?t={c['contract_token']}" in html and "no account needed" in html


def test_a_field_by_field_answer_points_to_their_messages(monkeypatch):
    v = _setup_vendor()
    chat = new_chat(v["headers"])
    c = _create_contract(v, conversation_id=chat)
    sent: list[dict] = []
    monkeypatch.setattr(field_negotiation_service, "send_email", lambda **kw: sent.append(kw) or {"success": True})
    db, booking, vendor = _loaded(c["booking_id"])
    assert field_negotiation_service._tell(booking, vendor, "vendor", [{}], db) is True
    db.close()
    html = sent[0]["html"]
    assert f"/conversation/?id={chat}" in html and "booking-link" not in html


def test_an_addendum_email_keeps_its_link_but_not_no_account_needed(monkeypatch):
    v = _setup_vendor()
    c = _signed_with_schedule(v)
    sent: list[dict] = []
    monkeypatch.setattr(document_service, "send_email", lambda **kw: sent.append(kw) or {"success": True})
    d = client.post(
        f"/contracts/{c['booking_id']}/documents",
        json={"kind": "addendum", "sections": [{"title": "What changes", "body": "Two more uplights."}],
              "send": True, "email_client": True},
        headers=v["headers"],
    )
    assert d.status_code in (200, 201), d.text
    html = sent[-1]["html"]
    assert f"booking-link?d={d.json()['token']}" in html
    assert "no account" not in html
