"""The vendor countersigns after the client (docs/DECISIONS.md #34): the
client's signature leaves a contract waiting on the vendor, holding its date
with no expiry; the vendor's signature books it and sends both sides the
PDF, or the vendor declines to sign with a reason, which voids it."""

from datetime import datetime, timedelta

import pytest

from app.db.models import Booking, ContractDocument, ContractEvent, Vendor
from app.services import countersign_service, esign_consent, pdf_service
from tests.test_api import TestingSessionLocal, client
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _create_contract,
    _isolate,
    _setup_vendor,
)

CONSENT = {"consent": True, "consent_version": esign_consent.VERSION}


@pytest.fixture
def outbox(monkeypatch):
    import app.services.guest_booking_service as gbs

    sent = []
    capture = lambda **kw: sent.append(kw) or {"success": True}  # noqa: E731
    for module in (gbs, countersign_service):
        monkeypatch.setattr(module, "send_email", capture)
    return sent


def _client_signs(c, name="Priya Mehta"):
    token = c["contract_token"]
    client.patch(f"/guest-bookings/{token}", json={"guest_email": "priya@example.com"})
    r = client.post(f"/guest-bookings/{token}/sign", json={"signer_name": name})
    assert r.status_code == 200, r.text
    return r.json()


def _countersign(v, c, **body):
    return client.post(
        f"/contracts/{c['booking_id']}/countersign", json={"signer_name": "Arjun Rao", **body}, headers=v["headers"],
    )


def _booking(booking_id) -> Booking:
    db = TestingSessionLocal()
    b = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    db.expunge(b)
    db.close()
    return b


def _events(booking_id, kind):
    db = TestingSessionLocal()
    rows = db.query(ContractEvent).filter(ContractEvent.booking_id == booking_id, ContractEvent.kind == kind).all()
    db.close()
    return rows


def _forget_consent(v):
    db = TestingSessionLocal()
    db.query(Vendor).filter(Vendor.vendor_id == v["vendor_id"]).update({Vendor.esign_consent_version: None})
    db.commit()
    db.close()


# ── The client signs first ───────────────────────────────────────────

def test_the_clients_signature_waits_on_the_vendor_and_holds_the_date(outbox):
    v = _setup_vendor()
    c = _create_contract(v, date_iso="2027-11-20")
    signed = _client_signs(c)
    assert signed["contract_status"] == "awaiting_vendor"
    assert signed["client_signed_at"] and signed["signed_at"] is None
    # No PDF yet: the client is told it's waiting on the vendor.
    assert not [m for m in outbox if m.get("attachments")]
    assert outbox[-1]["to"] == "priya@example.com" and "waiting for" in outbox[-1]["subject"]

    # Nothing is owed yet.
    assert client.post(f"/guest-bookings/{c['contract_token']}/mark-deposit-paid").status_code == 400

    # It's the vendor's move on Leads.
    items = client.get("/leads/pipeline", headers=v["headers"]).json()["items"]
    item = next(i for i in items if i["id"] == f"booking:{c['booking_id']}")
    assert (item["attention"], item["attention_reason"]) == ("needs_you", "countersign")

    # The date is held, and the hold doesn't lapse while it waits on the vendor.
    db = TestingSessionLocal()
    db.query(Booking).filter(Booking.booking_id == c["booking_id"]).update(
        {Booking.hold_expires_at: datetime.utcnow() - timedelta(days=1)},
    )
    db.commit()
    db.close()
    mine = client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()
    assert mine["contract_status"] == "awaiting_vendor"
    clash = client.post("/contracts", json={
        "service_id": v["service_id"], "date_iso": "2027-11-20", "time_start": "19:00",
        "time_end": "23:00", "amount_cents": 100_000,
    }, headers=v["headers"])
    assert clash.status_code == 409


def test_once_the_client_signs_nobody_can_change_it(outbox):
    v = _setup_vendor()
    c = _create_contract(v)
    _client_signs(c)
    bid, token = c["booking_id"], c["contract_token"]
    assert client.patch(f"/contracts/{bid}", json={"amount_cents": 1}, headers=v["headers"]).status_code == 400
    assert client.post(f"/contracts/{bid}/send", json={}, headers=v["headers"]).status_code == 400
    voided = client.post(f"/contracts/{bid}/void", headers=v["headers"])
    assert voided.status_code == 400 and "decline to sign" in voided.json()["detail"]
    assert client.post(f"/bookings/{bid}/archive", headers=v["headers"]).status_code == 400
    assert client.patch(f"/guest-bookings/{token}", json={"guest_name": "Someone"}).status_code == 400
    again = client.post(f"/guest-bookings/{token}/sign", json={"signer_name": "Priya Mehta"})
    assert again.status_code == 400 and "waiting for your vendor" in again.json()["detail"]
    assert client.post(f"/guest-bookings/{token}/decline", json={}).status_code == 400


# ── The vendor countersigns ──────────────────────────────────────────

def test_the_vendor_countersigns_once_with_consent_and_its_booked(outbox):
    v = _setup_vendor()
    _forget_consent(v)
    c = _create_contract(v)
    mine = client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()
    assert mine["vendor_esign_consent"] == {**esign_consent.view(), "given": False}
    signed = _client_signs(c)
    client_sha = signed["signed_snapshot_sha256"]

    no_consent = _countersign(v, c)
    assert no_consent.status_code == 400 and "agree to sign electronically" in no_consent.json()["detail"]
    stale = _countersign(v, c, consent=True, consent_version="old")
    assert stale.status_code == 409
    assert _countersign(v, c, signer_name="  ", **CONSENT).status_code == 400

    r = client.post(
        f"/contracts/{c['booking_id']}/countersign", json={"signer_name": "Arjun Rao", **CONSENT},
        headers={**v["headers"], "user-agent": "Vendor Chrome"},
    )
    assert r.status_code == 200, r.text
    booked = r.json()
    assert booked["contract_status"] == "signed"
    assert booked["signed_at"] and booked["vendor_signed_at"] == booked["signed_at"]
    assert booked["vendor_signer_name"] == "Arjun Rao"
    assert booked["vendor_esign_consent"]["given"] is True
    assert booked["signed_snapshot_sha256"] != client_sha

    b = _booking(c["booking_id"])
    counter = b.signed_snapshot["countersignature"]
    assert counter["client_sha256"] == client_sha
    assert counter["signer_name"] == "Arjun Rao"
    assert counter["evidence"]["user_agent"] == "Vendor Chrome"
    assert counter["evidence"]["signed_in_as"]["user_id"] == v["vendor_user_id"]
    assert counter["evidence"]["consent"]["version"] == esign_consent.VERSION
    # The client's signature is still there, under the same fingerprint as signed.
    assert b.signed_snapshot["signer_name"] == "Priya Mehta"
    assert b.vendor_seen_at is not None
    [event] = _events(c["booking_id"], "countersigned")
    assert event.detail["client_sha256"] == client_sha

    # Both sides get the PDF now, and the client's receipt names both.
    copies = [m for m in outbox if m.get("attachments")]
    assert len(copies) == 2
    receipt = next(m for m in copies if m["to"] == "priya@example.com")
    assert "Arjun Rao" in receipt["html"] and "Priya Mehta" in receipt["html"]

    # Payments can start; signing twice can't.
    assert client.post(f"/guest-bookings/{c['contract_token']}/mark-deposit-paid").status_code == 200
    assert _countersign(v, c, **CONSENT).status_code == 400

    # Consent is remembered for the next one.
    c2 = _create_contract(v, date_iso="2027-12-04")
    _client_signs(c2)
    assert _countersign(v, c2).status_code == 200


def test_only_the_vendor_and_only_after_the_client(outbox):
    v, other = _setup_vendor(), _setup_vendor()
    c = _create_contract(v)
    early = _countersign(v, c, **CONSENT)
    assert early.status_code == 400 and "hasn't signed" in early.json()["detail"]
    _client_signs(c)
    assert _countersign(other, c, **CONSENT).status_code == 404
    assert client.post(f"/contracts/{c['booking_id']}/countersign", json={"signer_name": "X"}).status_code in (401, 403)


def test_declining_to_sign_voids_it_frees_the_date_and_tells_the_client_why(outbox):
    v = _setup_vendor()
    c = _create_contract(v, date_iso="2027-11-27")
    _client_signs(c)
    url = f"/contracts/{c['booking_id']}/decline-signature"
    assert client.post(url, json={"reason": "   "}, headers=v["headers"]).status_code == 400

    r = client.post(url, json={"reason": "We're no longer free that night."}, headers=v["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["contract_status"] == "voided" and r.json()["status"] == "rejected"
    b = _booking(c["booking_id"])
    assert b.rejected_reason == "vendor_declined_signature"
    assert b.vendor_decline_note == "We're no longer free that night."
    told = outbox[-1]
    assert told["to"] == "priya@example.com" and "We're no longer free that night." in told["html"]

    # The date is free again, and nothing more can happen on it.
    again = _create_contract(v, date_iso="2027-11-27")
    assert again["contract_status"] == "sent"
    assert _countersign(v, c, **CONSENT).status_code == 400
    assert client.get(f"/guest-bookings/{c['contract_token']}").json()["status"] == "rejected"


# ── Attached documents ───────────────────────────────────────────────

def _signed_booking(v):
    c = _create_contract(v)
    _client_signs(c)
    assert _countersign(v, c, **CONSENT).status_code == 200
    return c


def _document(v, c):
    d = client.post(
        f"/contracts/{c['booking_id']}/documents",
        json={"kind": "addendum", "sections": [{"title": "Later finish", "body": "Until midnight."}], "send": True},
        headers=v["headers"],
    )
    assert d.status_code == 201, d.text
    return d.json()


def test_a_document_the_client_signed_waits_for_the_vendor(outbox, monkeypatch):
    import app.services.document_service as ds

    monkeypatch.setattr(ds, "send_email", lambda **kw: outbox.append(kw) or {"success": True})
    v = _setup_vendor()
    c = _signed_booking(v)
    d = _document(v, c)
    outbox.clear()
    r = client.post(f"/guest-documents/{d['token']}/sign", json={"signer_name": "Priya Mehta"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "awaiting_vendor" and r.json()["signed_at"] is None
    assert not [m for m in outbox if m.get("attachments")]

    did = d["document_id"]
    assert client.patch(f"/contract-documents/{did}", json={"title": "X"}, headers=v["headers"]).status_code == 400
    assert client.post(f"/contract-documents/{did}/send", json={}, headers=v["headers"]).status_code == 400
    assert client.post(f"/contract-documents/{did}/void", headers=v["headers"]).status_code == 400

    signed = client.post(f"/contract-documents/{did}/countersign", json={"signer_name": "Arjun Rao"}, headers=v["headers"])
    assert signed.status_code == 200, signed.text
    assert signed.json()["status"] == "signed" and signed.json()["vendor_signer_name"] == "Arjun Rao"
    assert {m["to"] for m in outbox if m.get("attachments")} == {"priya@example.com", outbox[-1]["to"]}
    db = TestingSessionLocal()
    row = db.query(ContractDocument).filter(ContractDocument.document_id == did).first()
    assert row.signed_snapshot["countersignature"]["signer_name"] == "Arjun Rao"
    db.close()


def test_declining_a_document_leaves_the_booking_as_it_was(outbox, monkeypatch):
    import app.services.document_service as ds

    monkeypatch.setattr(ds, "send_email", lambda **kw: outbox.append(kw) or {"success": True})
    v = _setup_vendor()
    c = _signed_booking(v)
    d = _document(v, c)
    client.post(f"/guest-documents/{d['token']}/sign", json={"signer_name": "Priya Mehta"})
    r = client.post(
        f"/contract-documents/{d['document_id']}/decline-signature",
        json={"reason": "Midnight doesn't work after all."}, headers=v["headers"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "voided"
    assert "Midnight doesn't work after all." in outbox[-1]["html"]
    assert client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()["contract_status"] == "signed"


# ── Reminders ────────────────────────────────────────────────────────

def test_the_vendor_is_reminded_a_day_and_three_days_after_then_never_again(outbox, monkeypatch):
    told = []
    monkeypatch.setattr(countersign_service, "notify_vendor_contract_event", lambda **kw: told.append(kw))
    v = _setup_vendor()
    c = _create_contract(v)
    _client_signs(c)
    signed_at = _booking(c["booking_id"]).client_signed_at.replace(tzinfo=None)

    def sweep(after):
        db = TestingSessionLocal()
        try:
            countersign_service.send_countersign_reminders(db=db, now=signed_at + after)
        finally:
            db.close()
        return [t for t in told if t["booking_id"] == c["booking_id"]]

    assert sweep(timedelta(hours=23)) == []
    assert len(sweep(timedelta(hours=25))) == 1
    assert len(sweep(timedelta(hours=30))) == 1
    mine = sweep(timedelta(days=3, hours=1))
    assert len(mine) == 2 and "3 days" in mine[-1]["title"]
    assert len(sweep(timedelta(days=10))) == 2

    # A countersigned one is never reminded.
    other = _create_contract(v, date_iso="2027-12-11")
    _client_signs(other)
    assert _countersign(v, other, **CONSENT).status_code == 200
    db = TestingSessionLocal()
    countersign_service.send_countersign_reminders(db=db, now=datetime.utcnow() + timedelta(days=5))
    db.close()
    assert not [t for t in told if t["booking_id"] == other["booking_id"]]


# ── The PDF ──────────────────────────────────────────────────────────

def test_the_pdf_signature_lines_follow_the_signatures(outbox, monkeypatch):
    lines = []
    real = pdf_service._signatures
    monkeypatch.setattr(
        pdf_service, "_signatures",
        lambda pdf, *a, **kw: lines.append((a, kw)) or real(pdf, *a, **kw),
    )

    def provider_line(c):
        lines.clear()
        assert client.get(f"/contracts/{c['booking_id']}/pdf", headers=v["headers"]).status_code == 200
        (_, _, signer, _, _, counter, awaiting), _ = lines[-1]
        return signer, counter, awaiting

    v = _setup_vendor()
    c = _create_contract(v)
    _client_signs(c)
    signer, counter, awaiting = provider_line(c)
    assert (signer, counter, awaiting) == ("Priya Mehta", None, True)

    assert _countersign(v, c, **CONSENT).status_code == 200
    signer, counter, awaiting = provider_line(c)
    assert counter["signer_name"] == "Arjun Rao" and awaiting is False

    # Signed before vendors countersigned: the old "issued" line, not "awaiting".
    old = _create_contract(v, date_iso="2027-12-18")
    _client_signs(old)
    db = TestingSessionLocal()
    b = db.query(Booking).filter(Booking.booking_id == old["booking_id"]).first()
    b.signed_at, b.contract_status = b.client_signed_at, "signed"
    db.commit()
    db.close()
    signer, counter, awaiting = provider_line(old)
    assert (counter, awaiting) == (None, False)
