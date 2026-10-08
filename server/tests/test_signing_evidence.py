"""What a signature by link carries (docs/DECISIONS.md #27): the emailed
code, the e-records consent, the address and browser, all inside the
hashed snapshot; a signing certificate in the PDF; and a signed copy to
both sides."""

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app import config
from app.db.models import Booking, ContractDocument, ContractEvent, SigningCode
from app.services import esign_consent
from tests.test_api import TestingSessionLocal, client
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _create_contract,
    _created,
    _isolate,
    _setup_vendor,
)

SECTIONS = [{"title": "New finish time", "body": "The reception now ends at midnight."}]


@pytest.fixture(autouse=True)
def _clean():
    yield
    db = TestingSessionLocal()
    if _created["bookings"]:
        ids = _created["bookings"]
        db.query(SigningCode).filter(SigningCode.booking_id.in_(ids)).delete(synchronize_session=False)
        db.query(ContractDocument).filter(ContractDocument.booking_id.in_(ids)).delete(synchronize_session=False)
        db.query(ContractEvent).filter(ContractEvent.booking_id.in_(ids)).delete(synchronize_session=False)
        db.commit()
    db.close()


@pytest.fixture
def required(monkeypatch):
    monkeypatch.setattr(config, "SIGNING_REQUIRE_CODE", True)


class Outbox:
    """Every email the signing flow sends, in place of Resend."""

    def __init__(self):
        self.sent = []

    def __call__(self, **kw):
        self.sent.append(kw)
        return {"success": True, "id": f"email-{len(self.sent)}"}

    def last_code(self) -> str:
        for m in reversed(self.sent):
            found = re.match(r"(\d{6}) is your code", m["subject"])
            if found:
                return found.group(1)
        raise AssertionError("no code was emailed")


@pytest.fixture
def outbox():
    box = Outbox()
    with patch("app.services.signing_evidence.send_email", box), \
            patch("app.services.guest_booking_service.send_email", box), \
            patch("app.services.document_service.send_email", box):
        yield box


def _contract(email="priya@example.com"):
    v = _setup_vendor()
    c = _create_contract(v, guest_email=email)
    return v, c, c["contract_token"]


def _sign(token, **body):
    return client.post(f"/guest-bookings/{token}/sign", json={"signer_name": "Priya Mehta", **body})


def _consent():
    return {"consent": True, "consent_version": esign_consent.VERSION}


def _booking(booking_id) -> Booking:
    db = TestingSessionLocal()
    b = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    db.close()
    return b


def _events(booking_id, kind):
    db = TestingSessionLocal()
    rows = db.query(ContractEvent).filter(ContractEvent.booking_id == booking_id, ContractEvent.kind == kind).all()
    db.close()
    return rows


def test_the_page_gets_the_consent_words_and_whether_a_code_is_needed():
    _, _, token = _contract()
    page = client.get(f"/guest-bookings/{token}").json()
    assert page["esign_consent"] == {"version": esign_consent.VERSION, "text": esign_consent.TEXT}
    assert page["signing_code_required"] is False


def test_signing_with_a_code_records_the_evidence_inside_the_fingerprint(outbox):
    _, c, token = _contract()
    client.get(f"/guest-bookings/{token}", headers={"user-agent": "Phone Safari"})
    sent = client.post(f"/guest-bookings/{token}/signing-code")
    assert sent.status_code == 200, sent.text
    assert sent.json()["sent_to"] == "p••••@example.com"
    assert outbox.sent[-1]["to"] == "priya@example.com"

    signed = _sign(token, code=outbox.last_code(), **_consent())
    assert signed.status_code == 200, signed.text

    b = _booking(c["booking_id"])
    ev = b.signed_snapshot["evidence"]
    assert ev["email"] == "priya@example.com"
    assert ev["email_verified_at"]
    assert ev["ip"] == "testclient"  # behind Railway, uvicorn puts X-Forwarded-For here
    assert ev["consent"]["version"] == esign_consent.VERSION
    assert ev["consent"]["text"] == esign_consent.TEXT
    assert signed.json()["signed_snapshot_sha256"] == b.signed_snapshot_sha256

    [viewed] = _events(c["booking_id"], "viewed")
    assert viewed.detail["user_agent"] == "Phone Safari"
    [signed_event] = _events(c["booking_id"], "signed")
    assert signed_event.detail["ip"] == "testclient"
    assert signed_event.detail["email_verified"] is True


def test_both_sides_get_the_signed_pdf(outbox):
    _, c, token = _contract()
    client.post(f"/guest-bookings/{token}/signing-code")
    _sign(token, code=outbox.last_code(), **_consent())
    copies = [m for m in outbox.sent if m.get("attachments")]
    assert {m["to"] for m in copies} == {"priya@example.com", f"{_vendor_email(c)}"}
    for m in copies:
        assert m["attachments"][0]["filename"].endswith(".pdf")
    assert {e.detail["to"] for e in _events(c["booking_id"], "copy_sent")} == {"client", "vendor"}


def _vendor_email(c):
    from app.db.models import User, Vendor

    db = TestingSessionLocal()
    vendor = db.query(Vendor).filter(Vendor.vendor_id == c["vendor_id"]).first()
    email = db.query(User).filter(User.user_id == vendor.user_id).first().email
    db.close()
    return email


def test_the_signed_pdf_ends_with_a_certificate_page(outbox):
    _, _, token = _contract()
    def pages(pdf: bytes) -> int:
        return len(re.findall(rb"/Type /Page\b", pdf))

    before = client.get(f"/guest-bookings/{token}/pdf").content
    client.post(f"/guest-bookings/{token}/signing-code")
    _sign(token, code=outbox.last_code(), **_consent())
    after = client.get(f"/guest-bookings/{token}/pdf").content
    assert pages(after) == pages(before) + 1


def test_a_wrong_code_is_refused_and_counted(outbox):
    _, c, token = _contract()
    client.post(f"/guest-bookings/{token}/signing-code")
    wrong = "000000" if outbox.last_code() != "000000" else "111111"
    r = _sign(token, code=wrong, **_consent())
    assert r.status_code == 400
    assert "isn't right" in r.json()["detail"]
    assert _booking(c["booking_id"]).signed_at is None
    for _ in range(4):
        _sign(token, code=wrong, **_consent())
    # Five wrong guesses spend the code, even the right one.
    r = _sign(token, code=outbox.last_code(), **_consent())
    assert r.status_code == 400
    assert "expired" in r.json()["detail"]


def test_a_new_code_replaces_the_old_one(outbox):
    _, _, token = _contract()
    client.post(f"/guest-bookings/{token}/signing-code")
    first = outbox.last_code()
    client.post(f"/guest-bookings/{token}/signing-code")
    second = outbox.last_code()
    if first != second:
        assert _sign(token, code=first, **_consent()).status_code == 400
    assert _sign(token, code=second, **_consent()).status_code == 200


def test_an_expired_code_is_refused(outbox):
    _, c, token = _contract()
    client.post(f"/guest-bookings/{token}/signing-code")
    db = TestingSessionLocal()
    db.query(SigningCode).filter(SigningCode.booking_id == c["booking_id"]).update(
        {SigningCode.expires_at: datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)},
    )
    db.commit()
    db.close()
    r = _sign(token, code=outbox.last_code(), **_consent())
    assert r.status_code == 400
    assert "expired" in r.json()["detail"]


def test_changing_the_email_voids_the_code(outbox):
    _, _, token = _contract()
    client.post(f"/guest-bookings/{token}/signing-code")
    code = outbox.last_code()
    client.patch(f"/guest-bookings/{token}", json={"guest_email": "someone-else@example.com"})
    r = _sign(token, code=code, **_consent())
    assert r.status_code == 400
    assert "email changed" in r.json()["detail"]


def test_out_of_date_consent_words_are_refused(outbox):
    _, _, token = _contract()
    client.post(f"/guest-bookings/{token}/signing-code")
    r = _sign(token, code=outbox.last_code(), consent=True, consent_version="2020-01-v0")
    assert r.status_code == 409


def test_when_required_a_signature_needs_both_the_code_and_the_consent(required, outbox):
    _, c, token = _contract()
    assert client.get(f"/guest-bookings/{token}").json()["signing_code_required"] is True
    r = _sign(token, **_consent())
    assert r.status_code == 400
    assert "code" in r.json()["detail"]
    client.post(f"/guest-bookings/{token}/signing-code")
    r = _sign(token, code=outbox.last_code())
    assert r.status_code == 400
    assert "Tick the box" in r.json()["detail"]
    # The refusal for consent didn't spend the code.
    assert _sign(token, code=outbox.last_code(), **_consent()).status_code == 200
    assert _booking(c["booking_id"]).signed_snapshot["evidence"]["email_verified_at"]


def test_while_not_required_an_older_page_still_signs_and_says_so():
    _, c, token = _contract()
    assert _sign(token).status_code == 200
    ev = _booking(c["booking_id"]).signed_snapshot["evidence"]
    assert ev["email_verified_at"] is None
    assert ev["consent"] is None
    assert ev["ip"] == "testclient"


def test_the_staging_test_code_signs(required, monkeypatch):
    monkeypatch.setattr(config, "SIGNING_TEST_CODE", "424242")
    _, _, token = _contract()
    assert _sign(token, code="424242", **_consent()).status_code == 200


def test_a_code_needs_an_email_on_file():
    v = _setup_vendor()
    c = _create_contract(v)
    r = client.post(f"/guest-bookings/{c['contract_token']}/signing-code")
    assert r.status_code == 400
    assert "email" in r.json()["detail"]


def test_a_failed_send_says_so():
    _, _, token = _contract()
    with patch("app.services.signing_evidence.send_email", return_value={"success": False, "error": "x"}):
        r = client.post(f"/guest-bookings/{token}/signing-code")
    assert r.status_code == 502


# ── Attached documents ───────────────────────────────────────────────

def test_a_document_signs_with_its_own_code_and_both_sides_get_a_copy(required, outbox):
    v, c, token = _contract()
    client.post(f"/guest-bookings/{token}/signing-code")
    _sign(token, code=outbox.last_code(), **_consent())
    d = client.post(
        f"/contracts/{c['booking_id']}/documents",
        json={"kind": "addendum", "sections": SECTIONS, "send": True}, headers=v["headers"],
    ).json()
    page = client.get(f"/guest-documents/{d['token']}").json()
    assert page["esign_consent"]["version"] == esign_consent.VERSION

    contract_code = outbox.last_code()
    sent = client.post(f"/guest-documents/{d['token']}/signing-code")
    assert sent.status_code == 200, sent.text
    doc_code = outbox.last_code()
    if contract_code != doc_code:
        # The contract's (spent) code isn't this document's.
        r = client.post(f"/guest-documents/{d['token']}/sign",
                        json={"signer_name": "Priya Mehta", "code": contract_code, **_consent()})
        assert r.status_code == 400

    outbox.sent.clear()
    r = client.post(f"/guest-documents/{d['token']}/sign",
                    json={"signer_name": "Priya Mehta", "code": doc_code, **_consent()})
    assert r.status_code == 200, r.text

    db = TestingSessionLocal()
    row = db.query(ContractDocument).filter(ContractDocument.document_id == d["document_id"]).first()
    ev = row.signed_snapshot["evidence"]
    db.close()
    assert ev["email_verified_at"] and ev["consent"]["version"] == esign_consent.VERSION
    assert {m["to"] for m in outbox.sent if m.get("attachments")} == {"priya@example.com", _vendor_email(c)}


def test_with_a_test_code_a_failed_send_still_lets_the_page_ask_for_it(required, monkeypatch):
    # Staging: no email, signs with SIGNING_TEST_CODE.
    monkeypatch.setattr(config, "SIGNING_TEST_CODE", "424242")
    _, c, token = _contract()
    with patch("app.services.signing_evidence.send_email", return_value={"success": False, "error": "Email not configured"}):
        r = client.post(f"/guest-bookings/{token}/signing-code")
    assert r.status_code == 200, r.text
    [sent] = _events(c["booking_id"], "code_sent")
    assert sent.detail["unsent"] is True
    assert _sign(token, code="424242", **_consent()).status_code == 200
