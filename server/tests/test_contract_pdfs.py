"""Contracts and attached documents as PDF downloads (docs/DECISIONS.md #22)."""

import pytest

from app.db.models import ContractDocument
from app.services import pdf_service
from tests.test_api import TestingSessionLocal, client
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _create_contract,
    _created,
    _isolate,
    _setup_vendor,
)

LAYOUT = [
    {"type": "parties"},
    {"id": "scope", "type": "terms", "title": "Scope", "body": "DJ and MC — 6 PM to midnight.\n\nIncludes “first dance” edit."},
    {"type": "event"}, {"type": "items"}, {"type": "schedule"}, {"type": "signature"},
]


@pytest.fixture(autouse=True)
def _clean_documents():
    yield
    db = TestingSessionLocal()
    if _created["bookings"]:
        db.query(ContractDocument).filter(ContractDocument.booking_id.in_(_created["bookings"])).delete(
            synchronize_session=False,
        )
        db.commit()
    db.close()


def _is_pdf(r, name_part: str):
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF-")
    assert "attachment;" in r.headers["content-disposition"]
    assert name_part in r.headers["content-disposition"]


def _sign(c):
    token = c["contract_token"]
    client.patch(f"/guest-bookings/{token}", json={"guest_email": "meera@example.com"})
    assert client.post(f"/guest-bookings/{token}/sign", json={"signer_name": "Meera Iyer"}).status_code == 200


def test_the_vendor_downloads_a_contract_before_and_after_signing():
    v = _setup_vendor()
    c = _create_contract(v, guest_email="meera@example.com", document_title="Reception DJ agreement", document_layout=LAYOUT)
    before = client.get(f"/contracts/{c['booking_id']}/pdf", headers=v["headers"])
    _is_pdf(before, "reception-dj-agreement-")
    _sign(c)
    after = client.get(f"/contracts/{c['booking_id']}/pdf", headers=v["headers"])
    _is_pdf(after, "reception-dj-agreement-")
    assert after.content != before.content


def test_only_the_vendor_and_the_link_can_download():
    v, other = _setup_vendor(), _setup_vendor()
    c = _create_contract(v)
    # Same answer as reading the contract itself.
    assert client.get(f"/contracts/{c['booking_id']}/pdf", headers=other["headers"]).status_code == 403
    assert client.get(f"/contracts/{c['booking_id']}/pdf").status_code in (401, 403)
    _is_pdf(client.get(f"/guest-bookings/{c['contract_token']}/pdf"), ".pdf")
    assert client.get("/guest-bookings/not-a-token/pdf").status_code == 404


def test_a_draft_has_no_public_pdf_but_the_vendor_can_preview_it():
    v = _setup_vendor()
    c = _create_contract(v, draft=True)
    assert client.get(f"/guest-bookings/{c['contract_token']}/pdf").status_code == 404
    _is_pdf(client.get(f"/contracts/{c['booking_id']}/pdf", headers=v["headers"]), ".pdf")


def test_downloading_doesnt_count_as_the_client_opening_it():
    v = _setup_vendor()
    c = _create_contract(v)
    client.get(f"/guest-bookings/{c['contract_token']}/pdf")
    assert client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()["contract_status"] == "sent"


def test_attached_documents_download_by_vendor_and_by_link():
    v = _setup_vendor()
    c = _create_contract(v, guest_email="meera@example.com")
    _sign(c)
    d = client.post(
        f"/contracts/{c['booking_id']}/documents",
        json={"kind": "addendum", "title": "Later finish", "sections": [{"title": "What changes", "body": "Ends at midnight."}]},
        headers=v["headers"],
    ).json()
    _is_pdf(client.get(f"/contract-documents/{d['document_id']}/pdf", headers=v["headers"]), "later-finish")
    assert client.get(f"/guest-documents/{d['token']}/pdf").status_code == 404  # still a draft
    client.post(f"/contract-documents/{d['document_id']}/send", json={}, headers=v["headers"])
    client.post(f"/guest-documents/{d['token']}/sign", json={"signer_name": "Meera Iyer"})
    _is_pdf(client.get(f"/guest-documents/{d['token']}/pdf"), "later-finish")


# ── Drawing, without the database ────────────────────────────────────

def test_blocks_follow_the_editor_and_never_drop_a_clause():
    clauses = [{"key": "a", "title": "A", "body": "a"}, {"key": "b", "title": "B", "body": "b"}]
    layout = [{"type": "items"}, {"id": "b", "type": "terms"}, {"type": "parties"}]
    order = [k if k != "terms" else c["key"] for k, c in pdf_service._ordered_blocks(layout, clauses)]
    assert order == ["items", "b", "parties", "event", "schedule", "a"]
    classic = [k for k, _ in pdf_service._ordered_blocks(None, clauses)]
    assert classic == ["parties", "event", "items", "schedule", "terms", "terms"]


def test_wording():
    assert pdf_service.money(123456) == "$1,234.56"
    assert pdf_service.pretty_time("18:05") == "6:05 PM"
    assert pdf_service.pretty_time("00:30") == "12:30 AM"
    assert pdf_service.describe_due({"due_type": "before_event", "due_days": 14}, None) == "Due 14 days before the event"
    assert pdf_service.filename("Meera & Arjun’s DJ", "2030-06-01") == "meera-arjun-s-dj-2030-06-01.pdf"


def test_renders_text_the_builtin_fonts_cant():
    content, _ = pdf_service.render_document(
        {"kind": "cancellation", "title": "Cancellation — ₹ refund", "sections": [{"title": "Über", "body": "“Quoted” — fine"}], "date_iso": "2030-06-01"},
        vendor_name="Sound Studio", client_name="Zoë Patel", agreement_title=None,
        signer="Zoë Patel", signed_at="2030-02-01T10:00:00+00:00", sha="ab" * 32,
    )
    assert content.startswith(b"%PDF-")
