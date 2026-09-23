"""Contract lifecycle additions on top of test_guest_booking_flow.py: client
details at creation, date sanity checks, editing through the double-booking
guard, voiding (and what a voided link still allows), and the vendor being
told when their client signs or says they've paid.
"""

from app.db.models import Booking, Lead
from tests.test_api import TestingSessionLocal, client
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _create_contract,
    _created,
    _isolate,
    _setup_vendor,
)


def _sign(token: str, email: str = "priya@example.com"):
    client.patch(f"/guest-bookings/{token}", json={"guest_email": email})
    resp = client.post(f"/guest-bookings/{token}/sign", json={"signer_name": "Priya Mehta"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_vendor_can_name_the_client_and_venue_up_front():
    v = _setup_vendor()
    contract = _create_contract(
        v, guest_name=" Priya Mehta ", guest_email="priya@example.com",
        guest_phone="7325550101", location="Pines Manor, Edison, NJ",
    )
    assert contract["guest_name"] == "Priya Mehta"
    assert contract["guest_email"] == "priya@example.com"
    assert contract["location"] == "Pines Manor, Edison, NJ"
    assert contract["status"] == "approved"

    # The client sees (and can still correct) what the vendor pre-filled.
    read = client.get(f"/guest-bookings/{contract['contract_token']}").json()
    assert read["guest_name"] == "Priya Mehta"
    assert read["location"] == "Pines Manor, Edison, NJ"


def test_rejects_a_past_date_or_an_end_before_the_start():
    v = _setup_vendor()
    base = {
        "service_id": v["service_id"], "time_start": "19:00", "time_end": "23:00",
        "amount_cents": 100_000,
    }
    past = client.post("/contracts", json={**base, "date_iso": "2020-01-01"}, headers=v["headers"])
    assert past.status_code == 400
    assert "past" in past.json()["detail"]

    backwards = client.post(
        "/contracts",
        json={**base, "date_iso": "2027-11-13", "date_end": "2027-11-12"},
        headers=v["headers"],
    )
    assert backwards.status_code == 400
    assert "end date" in backwards.json()["detail"]


def test_editing_a_contract_onto_a_booked_date_is_refused():
    v = _setup_vendor()
    _create_contract(v, date_iso="2027-12-01")
    movable = _create_contract(v, date_iso="2027-12-02")

    clash = client.patch(
        f"/contracts/{movable['booking_id']}", json={"date_iso": "2027-12-01"},
        headers=v["headers"],
    )
    assert clash.status_code == 409, clash.text

    # Its own date doesn't count against it.
    same = client.patch(
        f"/contracts/{movable['booking_id']}", json={"time_end": "23:30"}, headers=v["headers"],
    )
    assert same.status_code == 200, same.text


def test_voiding_frees_the_date_and_kills_the_link():
    v = _setup_vendor()
    contract = _create_contract(v, date_iso="2027-12-05")
    token = contract["contract_token"]

    voided = client.post(f"/contracts/{contract['booking_id']}/void", headers=v["headers"])
    assert voided.status_code == 200, voided.text
    assert voided.json()["status"] == "rejected"

    db = TestingSessionLocal()
    booking = db.query(Booking).filter(Booking.booking_id == contract["booking_id"]).first()
    assert booking.rejected_reason == "vendor_withdrew"
    db.close()

    # The date is free again.
    _create_contract(v, date_iso="2027-12-05")

    # The link still opens, so the client can see why, but nothing works.
    read = client.get(f"/guest-bookings/{token}")
    assert read.status_code == 200
    assert read.json()["status"] == "rejected"
    fill = client.patch(f"/guest-bookings/{token}", json={"guest_name": "Priya"})
    assert fill.status_code == 410
    sign = client.post(f"/guest-bookings/{token}/sign", json={"signer_name": "Priya"})
    assert sign.status_code == 410

    # Voiding twice is harmless; editing a voided contract isn't allowed.
    again = client.post(f"/contracts/{contract['booking_id']}/void", headers=v["headers"])
    assert again.status_code == 200
    edit = client.patch(
        f"/contracts/{contract['booking_id']}", json={"amount_cents": 1}, headers=v["headers"],
    )
    assert edit.status_code == 400


def test_a_signed_contract_cant_be_voided_or_voided_by_someone_else():
    v = _setup_vendor()
    other = _setup_vendor()
    contract = _create_contract(v)

    stranger = client.post(f"/contracts/{contract['booking_id']}/void", headers=other["headers"])
    assert stranger.status_code == 403

    _sign(contract["contract_token"])
    signed = client.post(f"/contracts/{contract['booking_id']}/void", headers=v["headers"])
    assert signed.status_code == 400


def test_vendor_is_told_when_the_client_signs_and_marks_payments(monkeypatch):
    import app.services.guest_booking_service as gbs

    sent = []
    monkeypatch.setattr(
        gbs, "notify_vendor_contract_event", lambda **kw: sent.append(kw) or {"sent": 0},
    )
    v = _setup_vendor()
    contract = _create_contract(v)
    token = contract["contract_token"]

    _sign(token)
    client.post(f"/guest-bookings/{token}/mark-deposit-paid")
    client.post(f"/guest-bookings/{token}/mark-paid")

    assert [s["event"] for s in sent] == [
        "contract_signed", "contract_deposit_marked_paid", "contract_marked_paid",
    ]
    assert all(s["vendor_user"].user_id == v["vendor_user_id"] for s in sent)
    assert "signed" in sent[0]["title"]


def test_converting_a_lead_keeps_what_the_vendor_typed_over_the_lead():
    v = _setup_vendor()
    lead = client.post(
        "/leads", json={"name": "Lead Name", "email": "lead@example.com", "phone": "111"},
        headers=v["headers"],
    ).json()

    resp = client.post(
        f"/leads/{lead['lead_id']}/convert",
        json={
            "service_id": v["service_id"], "date_iso": "2027-12-20", "time_start": "18:00",
            "time_end": "22:00", "amount_cents": 90_000, "guest_name": "Typed Name",
            "location": "Hall A",
        },
        headers=v["headers"],
    )
    assert resp.status_code == 201, resp.text
    _created["bookings"].append(resp.json()["booking_id"])
    got = client.get(f"/contracts/{resp.json()['booking_id']}", headers=v["headers"]).json()
    assert got["guest_name"] == "Typed Name"         # vendor's form wins
    assert got["guest_email"] == "lead@example.com"  # lead fills the blank
    assert got["location"] == "Hall A"

    db = TestingSessionLocal()
    db.query(Lead).filter(Lead.lead_id == lead["lead_id"]).delete()
    db.commit()
    db.close()
