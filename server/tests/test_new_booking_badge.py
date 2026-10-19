"""The Bookings tab's badge (docs/DECISIONS.md #30): a contract the client
signed before vendors countersigned (#34) became a booking without the vendor
doing anything, so it's new to them until they open it. One they countersign
themselves isn't new."""

from tests.countersign_helpers import countersign
from tests.test_api import TestingSessionLocal, client
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _create_contract,
    _isolate,
    _setup_vendor,
)
from app.db.models import Booking


def _signed(v, **overrides):
    """Signed the way a contract was before countersigning: the client's
    signature alone booked it."""
    c = _create_contract(v, guest_email="asha@example.com", guest_name="Asha Mehta", **overrides)
    r = client.post(f"/guest-bookings/{c['contract_token']}/sign", json={"signer_name": "Asha Mehta"})
    assert r.status_code == 200, r.text
    db = TestingSessionLocal()
    b = db.query(Booking).filter(Booking.booking_id == c["booking_id"]).first()
    b.signed_at, b.contract_status = b.client_signed_at, "signed"
    db.commit()
    db.close()
    return c


def _count(v):
    r = client.get("/bookings/vendor/new-count", headers=v["headers"])
    assert r.status_code == 200, r.text
    return r.json()["count"]


def test_a_signed_contract_is_new_until_the_vendor_opens_it():
    v = _setup_vendor()
    unsigned = _create_contract(v, guest_email="asha@example.com")
    assert _count(v) == 0, "an offer the client hasn't signed is still a lead"

    c = _signed(v, date_iso="2031-09-20")
    assert _count(v) == 1
    listed = client.get(f"/bookings/vendor/{v['vendor_id']}", headers=v["headers"]).json()["items"]
    row = next(b for b in listed if b["booking_id"] == c["booking_id"])
    assert row["vendor_seen_at"] is None

    r = client.post(f"/bookings/{c['booking_id']}/seen", headers=v["headers"])
    assert r.status_code == 200, r.text
    first = r.json()["vendor_seen_at"]
    assert first
    assert _count(v) == 0

    # Opening it again keeps the first time.
    assert client.post(f"/bookings/{c['booking_id']}/seen", headers=v["headers"]).json()["vendor_seen_at"] == first
    assert unsigned["booking_id"] != c["booking_id"]


def test_a_voided_booking_drops_off_the_badge():
    v = _setup_vendor()
    c = _signed(v)
    db = TestingSessionLocal()
    db.query(Booking).filter(Booking.booking_id == c["booking_id"]).update({Booking.status: "rejected"})
    db.commit()
    db.close()
    assert _count(v) == 0


def test_only_the_vendor_on_the_booking_can_mark_it_seen():
    v = _setup_vendor()
    other = _setup_vendor()
    c = _signed(v)
    assert client.post(f"/bookings/{c['booking_id']}/seen", headers=other["headers"]).status_code == 403
    assert client.post("/bookings/nope/seen", headers=v["headers"]).status_code == 404
    assert _count(v) == 1
    # Another vendor's count is their own.
    assert _count(other) == 0


def test_a_contract_the_vendor_countersigned_isnt_new_to_them():
    v = _setup_vendor()
    c = _create_contract(v, guest_email="asha@example.com", guest_name="Asha Mehta")
    assert client.post(
        f"/guest-bookings/{c['contract_token']}/sign", json={"signer_name": "Asha Mehta"},
    ).status_code == 200
    assert _count(v) == 0, "waiting on the vendor's signature, it's still a lead"
    assert countersign(c["booking_id"])["signed_at"]
    assert _count(v) == 0
