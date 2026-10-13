"""Who the signature says signed, against who the contract is for
(signing_evidence.name_check): a mismatch still signs, and is recorded in
the snapshot, the timeline and the certificate. And what the client can
change on the signing form: their own details, which the vendor sees, and
a venue or guest count only where the vendor left it blank."""

from unittest.mock import patch

import pytest

from app.services import pdf_service
from app.services.signing_evidence import name_check
from tests.test_api import client
from tests.test_signing_evidence import (  # noqa: F401 — _clean, _isolate and outbox are fixtures
    _booking,
    _clean,
    _consent,
    _create_contract,
    _events,
    _isolate,
    _setup_vendor,
    outbox,
)


@pytest.mark.parametrize(("typed", "matches"), [
    ("Asha Mehta", True),
    ("  asha   MEHTA ", True),
    ("Asha R. Mehta", True),
    ("Asha Rani Mehta", True),
    ("Bob Smith", False),
    ("Asha", False),
    ("Mehta Asha", False),
])
def test_name_check(typed, matches):
    assert name_check(typed, "Asha Mehta") == {"name_on_contract": "Asha Mehta", "name_matches": matches}


def test_with_no_name_on_the_contract_there_is_nothing_to_check():
    assert name_check("Bob Smith", None) == {"name_on_contract": None, "name_matches": None}
    assert name_check("Bob Smith", "  ") == {"name_on_contract": None, "name_matches": None}


def _contract(**overrides):
    v = _setup_vendor()
    c = _create_contract(v, guest_email="asha@example.com", guest_name="Asha Mehta", **overrides)
    return v, c, c["contract_token"]


def _sign_as(token, outbox, name):
    client.post(f"/guest-bookings/{token}/signing-code")
    return client.post(f"/guest-bookings/{token}/sign",
                       json={"signer_name": name, "code": outbox.last_code(), **_consent()})


def test_a_matching_signature_is_recorded_as_matching(outbox):
    _, c, token = _contract()
    assert _sign_as(token, outbox, "Asha R. Mehta").status_code == 200
    ev = _booking(c["booking_id"]).signed_snapshot["evidence"]
    assert ev["name_on_contract"] == "Asha Mehta"
    assert ev["name_matches"] is True
    [signed] = _events(c["booking_id"], "signed")
    assert "name_on_contract" not in signed.detail


def test_a_different_name_still_signs_and_is_flagged(outbox):
    _, c, token = _contract()
    r = _sign_as(token, outbox, "Bob Smith")
    assert r.status_code == 200, r.text
    b = _booking(c["booking_id"])
    assert b.signer_name == "Bob Smith"
    assert b.signed_snapshot["evidence"]["name_matches"] is False
    [signed] = _events(c["booking_id"], "signed")
    assert signed.detail["name_on_contract"] == "Asha Mehta"


def test_the_certificate_shows_a_mismatch(outbox):
    _, _, token = _contract()
    _sign_as(token, outbox, "Bob Smith")
    texts = []
    original = pdf_service._Pdf.multi_cell

    def capture(self, w, h=None, text="", *args, **kwargs):
        texts.append(text)
        return original(self, w, h, text, *args, **kwargs)

    with patch.object(pdf_service._Pdf, "multi_cell", capture):
        assert client.get(f"/guest-bookings/{token}/pdf").status_code == 200
    assert "Asha Mehta" in texts
    assert "The typed signature differs from the name on the contract" in texts


def test_a_document_signature_is_checked_against_the_client_name(outbox):
    v, c, token = _contract()
    _sign_as(token, outbox, "Asha Mehta")
    d = client.post(
        f"/contracts/{c['booking_id']}/documents",
        json={"kind": "addendum", "sections": [{"title": "Later finish", "body": "Until midnight."}], "send": True},
        headers=v["headers"],
    ).json()
    client.post(f"/guest-documents/{d['token']}/signing-code")
    r = client.post(f"/guest-documents/{d['token']}/sign",
                    json={"signer_name": "Bob Smith", "code": outbox.last_code(), **_consent()})
    assert r.status_code == 200, r.text
    [signed] = _events(c["booking_id"], "document_signed")
    assert signed.detail["name_on_contract"] == "Asha Mehta"


def _fill(token, **body):
    return client.patch(f"/guest-bookings/{token}", json=body)


def test_the_client_can_fill_in_a_blank_venue_and_guest_count():
    _, _, token = _contract()
    r = _fill(token, location="Pines Manor", guest_count=180)
    assert r.status_code == 200, r.text
    assert r.json()["location"] == "Pines Manor"
    assert r.json()["guest_count"] == 180


def test_a_set_venue_or_guest_count_needs_a_change_request():
    _, c, token = _contract(location="Pines Manor", guest_count=150)
    r = _fill(token, location="Somewhere else")
    assert r.status_code == 409
    assert "ask" in r.json()["detail"]
    assert _fill(token, location="").status_code == 409
    assert _fill(token, guest_count=180).status_code == 409
    b = _booking(c["booking_id"])
    assert (b.location, b.guest_count) == ("Pines Manor", 150)


def test_resending_the_same_venue_and_guest_count_is_fine():
    _, _, token = _contract(location="Pines Manor", guest_count=150)
    r = _fill(token, guest_name="Asha Mehta", location="Pines Manor", guest_count=150)
    assert r.status_code == 200, r.text


def test_changing_their_own_details_is_on_the_timeline():
    _, c, token = _contract()
    _fill(token, guest_name="Asha Mehta", guest_email="asha@example.com")
    assert _events(c["booking_id"], "details_updated") == []
    _fill(token, guest_name="Bob Smith", guest_phone="555-0100")
    [changed] = _events(c["booking_id"], "details_updated")
    # The phone was blank: filling it in isn't a change.
    assert changed.detail == {"fields": ["name"]}
    assert _booking(c["booking_id"]).guest_name == "Bob Smith"
