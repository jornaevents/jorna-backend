"""Contracts as offers (docs/DECISIONS.md #15): a sent contract holds its
date tentatively until hold_expires_at, a signed one for good, and a draft,
lapsed, declined or voided one not at all. Plus the viewed/declined states
and send/resend.
"""

from datetime import datetime, timedelta, timezone

from app.db.models import Booking
from tests.test_api import TestingSessionLocal, client
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _create_contract,
    _created,
    _isolate,
    _setup_vendor,
)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _set(booking_id: str, **fields):
    db = TestingSessionLocal()
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    for k, v in fields.items():
        setattr(booking, k, v)
    db.commit()
    db.close()


def _lapse(booking_id: str):
    _set(booking_id, hold_expires_at=_now() - timedelta(minutes=1))


def _sign(token: str):
    client.patch(f"/guest-bookings/{token}", json={"guest_email": "priya@example.com"})
    return client.post(f"/guest-bookings/{token}/sign", json={"signer_name": "Priya Mehta"})


def test_a_new_contract_is_sent_and_holds_its_date_for_seven_days():
    v = _setup_vendor()
    contract = _create_contract(v, date_iso="2027-10-01")
    assert contract["contract_status"] == "sent"
    held_for = datetime.fromisoformat(contract["hold_expires_at"]) - datetime.fromisoformat(
        contract["sent_at"]
    )
    assert held_for == timedelta(days=7)

    clash = client.post(
        "/contracts",
        json={"service_id": v["service_id"], "date_iso": "2027-10-01",
              "time_start": "19:00", "time_end": "23:00", "amount_cents": 100_000},
        headers=v["headers"],
    )
    assert clash.status_code == 409


def test_a_lapsed_hold_frees_the_date_and_the_offer_cant_be_signed():
    v = _setup_vendor()
    old = _create_contract(v, date_iso="2027-10-02")
    _lapse(old["booking_id"])

    assert client.get(f"/contracts/{old['booking_id']}", headers=v["headers"]).json()[
        "contract_status"
    ] == "expired"
    # The date is someone else's to take now.
    _create_contract(v, date_iso="2027-10-02")

    resp = _sign(old["contract_token"])
    assert resp.status_code == 410
    assert "expired" in resp.json()["detail"]


def test_resending_restarts_the_hold_but_only_if_the_date_is_still_free():
    v = _setup_vendor()
    free = _create_contract(v, date_iso="2027-10-03")
    _lapse(free["booking_id"])
    resent = client.post(f"/contracts/{free['booking_id']}/send", headers=v["headers"])
    assert resent.status_code == 200, resent.text
    assert resent.json()["contract_status"] == "sent"
    assert datetime.fromisoformat(resent.json()["hold_expires_at"]) > datetime.now(timezone.utc) + timedelta(days=6)
    assert _sign(free["contract_token"]).status_code == 200

    taken = _create_contract(v, date_iso="2027-10-04")
    _lapse(taken["booking_id"])
    _create_contract(v, date_iso="2027-10-04")
    clash = client.post(f"/contracts/{taken['booking_id']}/send", headers=v["headers"])
    assert clash.status_code == 409


def test_a_draft_holds_nothing_and_its_link_doesnt_work_until_sent():
    v = _setup_vendor()
    draft = _create_contract(v, date_iso="2027-10-05", draft=True)
    assert draft["contract_status"] == "draft"
    assert draft["hold_expires_at"] is None
    assert client.get(f"/guest-bookings/{draft['contract_token']}").status_code == 404

    # Nothing is held, so another offer can go out on the same date...
    other = _create_contract(v, date_iso="2027-10-05")
    # ...and then the draft can't be sent onto it.
    assert client.post(f"/contracts/{draft['booking_id']}/send", headers=v["headers"]).status_code == 409

    client.post(f"/contracts/{other['booking_id']}/void", headers=v["headers"])
    sent = client.post(
        f"/contracts/{draft['booking_id']}/send", json={"hold_days": 3}, headers=v["headers"],
    )
    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["contract_status"] == "sent"
    assert datetime.fromisoformat(body["hold_expires_at"]) - datetime.fromisoformat(
        body["sent_at"]
    ) == timedelta(days=3)
    assert client.get(f"/guest-bookings/{draft['contract_token']}").status_code == 200


def test_the_vendors_hold_window_is_their_default():
    v = _setup_vendor()
    resp = client.patch("/vendors/me", json={"contract_hold_days": 14}, headers=v["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["contract_hold_days"] == 14

    contract = _create_contract(v, date_iso="2027-10-06")
    assert datetime.fromisoformat(contract["hold_expires_at"]) - datetime.fromisoformat(
        contract["sent_at"]
    ) == timedelta(days=14)
    # A per-contract override still wins.
    short = _create_contract(v, date_iso="2027-10-07", hold_days=2)
    assert datetime.fromisoformat(short["hold_expires_at"]) - datetime.fromisoformat(
        short["sent_at"]
    ) == timedelta(days=2)


def test_opening_the_link_marks_it_viewed_but_the_vendors_preview_doesnt():
    v = _setup_vendor()
    contract = _create_contract(v, date_iso="2027-10-08")
    token = contract["contract_token"]

    preview = client.get(f"/guest-bookings/{token}?preview=true").json()
    assert preview["contract_status"] == "sent"

    opened = client.get(f"/guest-bookings/{token}").json()
    assert opened["contract_status"] == "viewed"
    mine = client.get(f"/contracts/{contract['booking_id']}", headers=v["headers"]).json()
    assert mine["viewed_at"] is not None

    # A viewed offer still holds, and signing moves it to signed.
    assert _sign(token).json()["contract_status"] == "signed"


def test_declining_frees_the_date_tells_the_vendor_and_ends_the_offer(monkeypatch):
    import app.services.guest_booking_service as gbs

    told = []
    monkeypatch.setattr(gbs, "notify_vendor_contract_event", lambda **kw: told.append(kw) or {})
    v = _setup_vendor()
    contract = _create_contract(v, date_iso="2027-10-09", guest_name="Priya Mehta")
    token = contract["contract_token"]

    resp = client.post(f"/guest-bookings/{token}/decline", json={"reason": "Went with a friend"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["contract_status"] == "declined"
    assert told[0]["event"] == "contract_declined"
    assert "Went with a friend" in told[0]["body"]

    _create_contract(v, date_iso="2027-10-09")  # the date is free again
    assert _sign(token).status_code == 410
    assert client.post(f"/contracts/{contract['booking_id']}/send", headers=v["headers"]).status_code == 400
    assert client.patch(
        f"/contracts/{contract['booking_id']}", json={"amount_cents": 1}, headers=v["headers"],
    ).status_code == 400

    listed = client.get(f"/bookings/vendor/{v['vendor_id']}", headers=v["headers"]).json()["items"]
    row = next(b for b in listed if b["booking_id"] == contract["booking_id"])
    assert row["contract_status"] == "declined"
    assert row["rejected_reason"] == "client_declined"


def test_signing_refuses_a_date_that_was_taken_while_the_hold_was_down():
    """A hold that lapsed and was put back by hand (or a pre-hold contract)
    can find its date gone — signing is the hard block, so it checks."""
    v = _setup_vendor()
    first = _create_contract(v, date_iso="2027-10-10")
    _lapse(first["booking_id"])
    second = _create_contract(v, date_iso="2027-10-10")
    assert _sign(second["contract_token"]).status_code == 200

    _set(first["booking_id"], hold_expires_at=_now() + timedelta(days=1))
    resp = _sign(first["contract_token"])
    assert resp.status_code == 409


def test_voiding_records_when():
    v = _setup_vendor()
    contract = _create_contract(v, date_iso="2027-10-11")
    voided = client.post(f"/contracts/{contract['booking_id']}/void", headers=v["headers"]).json()
    assert voided["contract_status"] == "voided"
    assert voided["voided_at"] is not None
