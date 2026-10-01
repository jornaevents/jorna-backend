"""The document editor's layout, template kinds, and addenda / cancellation
agreements attached to a signed booking (docs/DECISIONS.md #21)."""

import pytest

from app.db.models import Booking, ContractDocument
from tests.test_api import TestingSessionLocal, client
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _create_contract,
    _created,
    _isolate,
    _setup_vendor,
)

LAYOUT = [
    {"id": "p", "type": "parties"},
    {"id": "intro", "type": "terms", "title": "Scope", "body": "DJ and MC for the reception."},
    {"id": "e", "type": "event"},
    {"id": "i", "type": "items"},
    {"id": "travel", "type": "terms", "title": "Travel", "body": "30 miles included."},
    {"id": "s", "type": "schedule"},
    {"id": "sig", "type": "signature"},
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


def _sign(contract: dict) -> None:
    token = contract["contract_token"]
    client.patch(f"/guest-bookings/{token}", json={"guest_email": "meera@example.com"})
    assert client.post(f"/guest-bookings/{token}/sign", json={"signer_name": "Meera Shah"}).status_code == 200


# ── Layout ───────────────────────────────────────────────────────────

def test_layout_is_stored_and_its_terms_become_the_clauses():
    v = _setup_vendor()
    c = _create_contract(v, document_title="Wedding DJ agreement", document_layout=LAYOUT)
    assert c["document_title"] == "Wedding DJ agreement"
    assert [b["type"] for b in c["document_layout"]] == ["parties", "terms", "event", "items", "terms", "schedule", "signature"]
    # The signing page reads clauses — the editor's terms, in its order.
    assert [(x["key"], x["title"]) for x in c["terms_clauses"]] == [("intro", "Scope"), ("travel", "Travel")]
    assert "body" not in c["document_layout"][1]  # text lives once, in the clauses

    edited = client.patch(
        f"/contracts/{c['booking_id']}",
        json={"document_layout": [{"id": "only", "type": "terms", "title": "Scope", "body": "Just the DJ."}, {"type": "signature"}]},
        headers=v["headers"],
    ).json()
    assert [x["body"] for x in edited["terms_clauses"]] == ["Just the DJ."]


def test_the_vendors_booking_list_carries_the_title():
    v = _setup_vendor()
    c = _create_contract(v, document_title="Wedding DJ agreement")
    rows = client.get(f"/bookings/vendor/{v['vendor_id']}", headers=v["headers"]).json()["items"]
    assert next(b for b in rows if b["booking_id"] == c["booking_id"])["document_title"] == "Wedding DJ agreement"


def test_layout_rejects_unknown_or_repeated_blocks():
    v = _setup_vendor()
    bad = client.post(
        "/contracts",
        json={
            "service_id": v["service_id"], "date_iso": "2027-11-13", "time_start": "19:00", "time_end": "23:00",
            "amount_cents": 100_000, "document_layout": [{"type": "items"}, {"type": "items"}],
        },
        headers=v["headers"],
    )
    assert bad.status_code == 400
    assert "only one items" in bad.json()["detail"]


def test_signing_freezes_the_title_and_layout():
    v = _setup_vendor()
    c = _create_contract(v, guest_email="meera@example.com", document_title="Wedding DJ agreement", document_layout=LAYOUT)
    _sign(c)
    db = TestingSessionLocal()
    snap = db.query(Booking).filter(Booking.booking_id == c["booking_id"]).first().signed_snapshot
    db.close()
    assert snap["document_title"] == "Wedding DJ agreement"
    assert snap["document_layout"][1]["id"] == "intro"


def test_template_kinds():
    v = _setup_vendor()
    made = client.post(
        "/contract-templates", json={"name": "Change of times", "body": {"version": 1}, "kind": "addendum"},
        headers=v["headers"],
    )
    assert made.status_code == 201
    assert made.json()["kind"] == "addendum"
    assert client.post(
        "/contract-templates", json={"name": "Nope", "body": {}, "kind": "invoice"}, headers=v["headers"],
    ).status_code == 400
    listed = client.get("/contract-templates", headers=v["headers"]).json()["items"]
    assert any(t["kind"] == "addendum" for t in listed)
    client.delete(f"/contract-templates/{made.json()['template_id']}", headers=v["headers"])


# ── Attached documents ───────────────────────────────────────────────

SECTIONS = [{"title": "New finish time", "body": "The reception now ends at midnight; nothing else changes."}]


def test_a_document_attaches_only_to_a_signed_booking():
    v = _setup_vendor()
    unsigned = _create_contract(v)
    r = client.post(
        f"/contracts/{unsigned['booking_id']}/documents",
        json={"kind": "addendum", "sections": SECTIONS}, headers=v["headers"],
    )
    assert r.status_code == 400
    assert "signed booking" in r.json()["detail"]


def test_addendum_lifecycle_without_touching_the_booking():
    v = _setup_vendor()
    c = _create_contract(v, guest_email="meera@example.com")
    _sign(c)
    before = client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()

    draft = client.post(
        f"/contracts/{c['booking_id']}/documents",
        json={"kind": "addendum", "title": "Later finish", "sections": SECTIONS}, headers=v["headers"],
    )
    assert draft.status_code == 201, draft.text
    d = draft.json()
    assert d["status"] == "draft"
    # A draft's link doesn't exist yet.
    assert client.get(f"/guest-documents/{d['token']}").status_code == 404

    edited = client.patch(
        f"/contract-documents/{d['document_id']}",
        json={"sections": SECTIONS + [{"title": "Overtime", "body": "Billed as before."}]}, headers=v["headers"],
    ).json()
    assert len(edited["sections"]) == 2

    sent = client.post(f"/contract-documents/{d['document_id']}/send", json={}, headers=v["headers"]).json()
    assert sent["status"] == "sent"

    opened = client.get(f"/guest-documents/{d['token']}").json()
    assert opened["status"] == "viewed"
    assert opened["title"] == "Later finish"
    assert "token" not in opened

    signed = client.post(f"/guest-documents/{d['token']}/sign", json={"signer_name": "Meera Shah"})
    assert signed.status_code == 200, signed.text
    assert signed.json()["signed_snapshot_sha256"]
    assert client.post(f"/guest-documents/{d['token']}/sign", json={"signer_name": "Again"}).status_code == 400
    assert client.post(f"/contract-documents/{d['document_id']}/void", headers=v["headers"]).status_code == 400

    after = client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()
    for field in ("amount_cents", "date_iso", "contract_status", "hold_expires_at"):
        assert after[field] == before[field]
    kinds = [e["kind"] for e in after["timeline"]]
    assert "document_sent" in kinds and "document_signed" in kinds

    listed = client.get(f"/contracts/{c['booking_id']}/documents", headers=v["headers"]).json()
    assert listed["total"] == 1


def test_cancellation_can_be_declined_or_voided():
    v = _setup_vendor()
    c = _create_contract(v, guest_email="meera@example.com")
    _sign(c)

    one = client.post(
        f"/contracts/{c['booking_id']}/documents",
        json={"kind": "cancellation", "sections": SECTIONS, "send": True}, headers=v["headers"],
    ).json()
    assert one["title"] == "Cancellation agreement"
    declined = client.post(f"/guest-documents/{one['token']}/decline", json={"reason": "We'd rather keep the date"}).json()
    assert declined["status"] == "declined"
    assert client.post(f"/guest-documents/{one['token']}/sign", json={"signer_name": "M S"}).status_code == 410

    two = client.post(
        f"/contracts/{c['booking_id']}/documents",
        json={"kind": "cancellation", "sections": SECTIONS, "send": True}, headers=v["headers"],
    ).json()
    voided = client.post(f"/contract-documents/{two['document_id']}/void", headers=v["headers"]).json()
    assert voided["status"] == "voided"
    assert client.post(f"/guest-documents/{two['token']}/sign", json={"signer_name": "M S"}).status_code == 410


def test_only_the_vendor_can_attach_or_edit():
    v = _setup_vendor()
    other = _setup_vendor()
    c = _create_contract(v, guest_email="meera@example.com")
    _sign(c)
    assert client.post(
        f"/contracts/{c['booking_id']}/documents",
        json={"kind": "addendum", "sections": SECTIONS}, headers=other["headers"],
    ).status_code == 404
    d = client.post(
        f"/contracts/{c['booking_id']}/documents",
        json={"kind": "addendum", "sections": SECTIONS}, headers=v["headers"],
    ).json()
    assert client.patch(
        f"/contract-documents/{d['document_id']}", json={"title": "Mine now"}, headers=other["headers"],
    ).status_code == 404
