"""Field-by-field contract negotiation (docs/DECISIONS.md #26): each side
answers the other's changes one field at a time, in strict turns, and the
contract only ever holds agreed values."""

import pytest

from app.db.models import (
    Booking,
    ContractDraft,
    ContractEvent,
    ContractProposal,
    ContractRevision,
    NegotiationField,
    NegotiationSend,
    Vendor,
)
from tests.test_api import TestingSessionLocal, client
from tests.test_change_proposals import LAYOUT
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _create_contract,
    _created,
    _isolate,
    _setup_vendor,
)


@pytest.fixture(autouse=True)
def _fields_on(monkeypatch):
    monkeypatch.setattr("app.config.FIELD_NEGOTIATION", True)
    yield
    db = TestingSessionLocal()
    if _created["bookings"]:
        for model in (NegotiationField, NegotiationSend, ContractDraft, ContractProposal, ContractRevision, ContractEvent):
            db.query(model).filter(model.booking_id.in_(_created["bookings"])).delete(synchronize_session=False)
        db.commit()
    db.close()


def _contract(v, **overrides) -> dict:
    """Two lines, a deposit and a balance, two clauses."""
    body = dict(
        line_items=[
            {"id": "pkg", "kind": "package", "service_id": v["service_id"], "unit_price_cents": 140_000, "quantity": 1},
            {"id": "lights", "kind": "custom", "name": "Uplighting", "unit_price_cents": 10_000, "quantity": 2},
        ],
        payment_schedule=[
            {"id": "dep", "label": "Deposit", "amount_cents": 50_000, "due_type": "on_signing"},
            {"id": "bal", "label": "Final balance", "amount_cents": 110_000, "due_type": "before_event", "due_days": 14},
        ],
        document_layout=LAYOUT,
        amount_cents=None,
        deposit_percent=None,
    )
    body.update(overrides)
    c = _create_contract(v, **body)
    client.patch(f"/guest-bookings/{c['contract_token']}", json={"guest_email": "meera@example.com"})
    return c


def _client_send(c, base_round, answers, message=None):
    return client.post(
        f"/guest-bookings/{c['contract_token']}/negotiation/send",
        json={"base_round": base_round, "answers": answers, "message": message},
    )


def _vendor_send(c, v, base_round, answers):
    return client.post(
        f"/contracts/{c['booking_id']}/negotiation/send",
        json={"base_round": base_round, "answers": answers}, headers=v["headers"],
    )


def _field(state, key):
    return next(f for f in state["fields"] if f["key"] == key)


def _booking(c) -> Booking:
    db = TestingSessionLocal()
    b = db.query(Booking).filter(Booking.booking_id == c["booking_id"]).first()
    db.expunge(b)
    db.close()
    return b


def test_a_new_contract_starts_at_round_one_with_the_client_to_move():
    v = _setup_vendor()
    c = _contract(v)
    s = client.get(f"/guest-bookings/{c['contract_token']}/negotiation").json()
    assert (s["round"], s["turn"], s["can_sign"]) == (1, "client", True)
    assert _field(s, "line:lights.quantity") == {**_field(s, "line:lights.quantity"), "value": 2, "state": "agreed"}
    assert _field(s, "schedule")["can_change"] is False
    assert _field(s, "event.date")["label"] == "Event date"
    assert _field(s, "line:lights.price")["label"] == "Uplighting: price"


def test_the_flag_off_keeps_the_earlier_proposals(monkeypatch):
    monkeypatch.setattr("app.config.FIELD_NEGOTIATION", False)
    v = _setup_vendor()
    c = _contract(v)
    r = client.get(f"/guest-bookings/{c['contract_token']}/negotiation")
    assert r.status_code == 409


def test_a_round_trip_settles_some_fields_and_leaves_others_open():
    v = _setup_vendor()
    c = _contract(v)

    # Round 2: the client asks for three lights and a later start.
    r = _client_send(c, 1, [
        {"key": "line:lights.quantity", "action": "change", "value": 3},
        {"key": "event.time", "action": "change", "value": {"time_start": "20:00", "time_end": "23:00"}},
    ], message="One more light, and a later start?")
    assert r.status_code == 200, r.text
    s = r.json()
    assert (s["round"], s["turn"], s["can_sign"]) == (2, "vendor", False)
    assert _field(s, "line:lights.quantity")["state"] == "waiting_vendor"
    # Nothing in the contract moves while it's only an ask.
    assert _field(s, "line:lights.quantity")["value"] == 2

    # It's the vendor's turn, so the client can't send again.
    assert _client_send(c, 2, [{"key": "discount", "action": "change", "value": 5_000}]).status_code == 409

    # Round 3: the vendor accepts the light and keeps their start time.
    r = _vendor_send(c, v, 2, [
        {"key": "line:lights.quantity", "action": "accept"},
        {"key": "event.time", "action": "keep", "note": "The venue opens at 7."},
    ])
    assert r.status_code == 200, r.text
    s = r.json()
    assert (s["round"], s["turn"]) == (3, "client")
    assert _field(s, "line:lights.quantity")["state"] == "settled"
    assert _field(s, "line:lights.quantity")["value"] == 3
    assert _field(s, "event.time")["state"] == "waiting_client"
    assert _field(s, "event.time")["proposed"] == {"time_start": "19:00", "time_end": "23:00"}

    # The settled change is in the contract, and the schedule followed the
    # new total ($1,700), keeping the deposit near its share.
    b = _booking(c)
    assert b.amount_cents == 170_000
    assert sum(i["amount_cents"] for i in b.payment_schedule) == 170_000
    assert b.payment_schedule[0]["amount_cents"] == round(50_000 * 170_000 / 160_000)
    assert b.revision == 2

    # Round 4: the client accepts the vendor's time, and can now sign.
    s = _client_send(c, 3, [{"key": "event.time", "action": "accept"}]).json()
    assert s["can_sign"] is True
    r = client.post(f"/guest-bookings/{c['contract_token']}/sign", json={"signer_name": "Meera Iyer", "revision": s["revision"]})
    assert r.status_code == 200, r.text


def test_everything_waiting_on_you_has_to_be_answered():
    v = _setup_vendor()
    c = _contract(v)
    _client_send(c, 1, [
        {"key": "line:lights.quantity", "action": "change", "value": 3},
        {"key": "discount", "action": "change", "value": 5_000},
    ])
    r = _vendor_send(c, v, 2, [{"key": "line:lights.quantity", "action": "accept"}])
    assert r.status_code == 400
    assert "Discount" in r.json()["detail"]


def test_the_client_cant_touch_the_schedule_or_a_locked_group():
    v = _setup_vendor()
    client.patch("/vendors/me", json={"negotiation_locks": ["prices"]}, headers=v["headers"])
    c = _contract(v)
    s = client.get(f"/guest-bookings/{c['contract_token']}/negotiation").json()
    assert s["locks"] == ["prices"]
    assert _field(s, "line:pkg.price")["locked"] is True

    r = _client_send(c, 1, [{"key": "schedule", "action": "change", "value": []}])
    assert r.status_code == 403
    r = _client_send(c, 1, [{"key": "line:pkg.price", "action": "change", "value": 120_000}])
    assert r.status_code == 403
    # Quantities aren't prices, so they stay open.
    assert _client_send(c, 1, [{"key": "line:lights.quantity", "action": "change", "value": 1}]).status_code == 200


def test_locks_are_copied_when_the_contract_is_sent():
    v = _setup_vendor()
    client.patch("/vendors/me", json={"negotiation_locks": ["event"]}, headers=v["headers"])
    c = _contract(v)
    client.patch("/vendors/me", json={"negotiation_locks": []}, headers=v["headers"])
    s = client.get(f"/contracts/{c['booking_id']}/negotiation", headers=v["headers"]).json()
    assert s["locks"] == ["event"]


def test_a_settled_field_stays_settled_unless_the_vendor_reopens_it():
    v = _setup_vendor()
    c = _contract(v)
    _client_send(c, 1, [{"key": "discount", "action": "change", "value": 10_000}])
    _vendor_send(c, v, 2, [{"key": "discount", "action": "accept"}])
    # Round 3 is the client's: they can't reopen it, or change it again.
    assert _client_send(c, 3, [{"key": "discount", "action": "reopen", "value": 20_000}]).status_code == 403
    assert _client_send(c, 3, [{"key": "discount", "action": "change", "value": 20_000}]).status_code == 400
    assert _client_send(c, 3, [{"key": "event.guests", "action": "change", "value": 200}]).status_code == 200
    # Round 5, the vendor's: they reopen it with a new value.
    r = _vendor_send(c, v, 4, [
        {"key": "event.guests", "action": "accept"},
        {"key": "discount", "action": "reopen", "value": 5_000},
    ])
    assert r.status_code == 200, r.text
    assert _field(r.json(), "discount")["state"] == "waiting_client"


def test_keep_mine_lets_the_other_side_ask_again():
    v = _setup_vendor()
    c = _contract(v)
    _client_send(c, 1, [{"key": "discount", "action": "change", "value": 10_000}])
    _vendor_send(c, v, 2, [{"key": "discount", "action": "keep"}])
    r = _client_send(c, 3, [{"key": "discount", "action": "counter", "value": 5_000}])
    assert r.status_code == 200
    assert _field(r.json(), "discount")["state"] == "waiting_vendor"


def test_signing_waits_for_nothing_open_unless_signed_as_it_is():
    v = _setup_vendor()
    c = _contract(v)
    _client_send(c, 1, [{"key": "discount", "action": "change", "value": 10_000}])
    _vendor_send(c, v, 2, [
        {"key": "discount", "action": "keep"},
        {"key": "policy.overtime", "action": "change", "value": 20_000},
    ])
    rev = client.get(f"/guest-bookings/{c['contract_token']}/negotiation").json()["revision"]
    r = client.post(f"/guest-bookings/{c['contract_token']}/sign", json={"signer_name": "Meera Iyer", "revision": rev})
    assert r.status_code == 409

    r = client.post(
        f"/guest-bookings/{c['contract_token']}/sign",
        json={"signer_name": "Meera Iyer", "revision": rev, "as_is": True},
    )
    assert r.status_code == 200, r.text
    b = _booking(c)
    # The vendor's open change is taken; the client's own ask is dropped.
    assert b.overtime_rate_cents == 20_000
    assert not b.discount_cents
    assert b.signed_at is not None


def test_the_client_can_add_one_of_the_vendors_packages():
    v = _setup_vendor()
    c = _contract(v)
    r = _client_send(c, 1, [{"key": "line:new:extra", "action": "change", "value": {"service_id": v["service_id"], "quantity": 1}}])
    assert r.status_code == 200, r.text
    assert _field(r.json(), "line:new:extra")["label"] == "Add 4-Hour Reception Package"
    r = _vendor_send(c, v, 2, [{"key": "line:new:extra", "action": "accept"}])
    assert r.status_code == 200, r.text
    assert len(_booking(c).line_items) == 3

    custom = _client_send(c, 3, [{"key": "line:new:mine", "action": "change", "value": {"name": "Fog machine"}}])
    assert custom.status_code == 400


def test_a_stale_round_and_the_old_endpoints_are_refused():
    v = _setup_vendor()
    c = _contract(v)
    _client_send(c, 1, [{"key": "discount", "action": "change", "value": 10_000}])
    assert _vendor_send(c, v, 1, [{"key": "discount", "action": "accept"}]).status_code == 409
    old = client.post(
        f"/guest-bookings/{c['contract_token']}/proposals",
        json={"base_revision": 1, "changes": {"discount_cents": 1}},
    )
    assert old.status_code == 409
    # And the contract can't be edited around an open ask.
    edit = client.patch(f"/contracts/{c['booking_id']}", json={"overtime_rate_cents": 1}, headers=v["headers"])
    assert edit.status_code == 409


def test_drafts_are_kept_per_side_and_dropped_on_send():
    v = _setup_vendor()
    c = _contract(v)
    answers = [{"key": "discount", "action": "change", "value": 10_000}]
    r = client.put(f"/guest-bookings/{c['contract_token']}/negotiation/draft", json={"answers": answers, "message": "hi"})
    assert r.status_code == 200
    s = client.get(f"/guest-bookings/{c['contract_token']}/negotiation").json()
    assert s["draft"]["answers"] == answers and s["draft"]["stale"] is False
    _client_send(c, 1, answers)
    assert client.get(f"/guest-bookings/{c['contract_token']}/negotiation").json()["draft"] is None


def test_the_nudge_shows_from_round_six():
    v = _setup_vendor()
    c = _contract(v)
    rnd = 1
    for _ in range(3):
        _client_send(c, rnd, [{"key": "discount", "action": "change" if rnd == 1 else "counter", "value": 1_000 * rnd}])
        rnd += 1
        _vendor_send(c, v, rnd, [{"key": "discount", "action": "counter", "value": 500 * rnd}])
        rnd += 1
    s = client.get(f"/guest-bookings/{c['contract_token']}/negotiation").json()
    assert s["round"] == 7 and s["nudge"] is True


def test_vendor_locks_are_validated():
    v = _setup_vendor()
    bad = client.patch("/vendors/me", json={"negotiation_locks": ["everything"]}, headers=v["headers"])
    assert bad.status_code == 422
    ok = client.patch("/vendors/me", json={"negotiation_locks": ["prices", "clauses"]}, headers=v["headers"])
    assert ok.json()["negotiation_locks"] == ["prices", "clauses"]
    db = TestingSessionLocal()
    assert db.query(Vendor).filter(Vendor.vendor_id == v["vendor_id"]).first().negotiation_locks == ["prices", "clauses"]
    db.close()


def test_each_side_is_offered_the_packages_it_may_add():
    from app.db.models import Service

    v = _setup_vendor()
    db = TestingSessionLocal()
    private = Service(name="Private after-party set", price=900.0, price_unit="event", vendor_id=v["vendor_id"],
                      experience="e", category="music", negotiable=False, status="hidden",
                      add_ons=[{"id": "fog", "name": "Fog machine", "price": 75, "price_unit": "event"}])
    db.add(private)
    db.commit()
    private_id = private.service_id
    db.close()
    _created["services"].append(private_id)

    c = _contract(v)
    mine = client.get(f"/guest-bookings/{c['contract_token']}/negotiation").json()["packages"]
    assert [p["service_id"] for p in mine] == [v["service_id"]]
    assert mine[0]["price_cents"] == 140_000

    # A private package isn't the client's to ask for, even by id.
    r = _client_send(c, 1, [{"key": "line:new:x", "action": "change", "value": {"service_id": private_id}}])
    assert r.status_code == 400
    assert _client_send(c, 1, [{"key": "line:new:y", "action": "change", "value": {"service_id": v["service_id"]}}]).status_code == 200

    # The vendor, on their turn, sees their private packages too.
    theirs = client.get(f"/contracts/{c['booking_id']}/negotiation", headers=v["headers"]).json()["packages"]
    assert {p["service_id"] for p in theirs} == {v["service_id"], private_id}
    assert theirs[[p["service_id"] for p in theirs].index(private_id)]["add_ons"] == [{"id": "fog", "name": "Fog machine", "price_cents": 7500}]
    # And whoever's waiting gets none.
    assert client.get(f"/guest-bookings/{c['contract_token']}/negotiation").json()["packages"] == []
