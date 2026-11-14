"""Unfinished drafts (2026-11-14): the vendor web editor saves a contract
draft as the vendor works, so a draft has to save before it's finished — no
date or hours yet, a line not named or priced, payments that don't add up —
and keep one version however often it's saved. Sending checks all of it.
"""

from tests.chat_helpers import new_chat
from tests.test_api import client
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _created,
    _isolate,
    _setup_vendor,
)


def _package(v, **line):
    return {"kind": "package", "service_id": v["service_id"], "quantity": 1, **line}


def _draft(v, **body):
    payload = {
        "draft": True,
        "date_iso": "",
        "time_start": "",
        "time_end": "",
        "line_items": [_package(v, unit_price_cents=120_000)],
        "conversation_id": new_chat(v["headers"]),
        **body,
    }
    resp = client.post("/contracts", json=payload, headers=v["headers"])
    assert resp.status_code == 201, resp.text
    _created["bookings"].append(resp.json()["booking_id"])
    return resp.json()


def _patch(v, booking_id, **body):
    return client.patch(f"/contracts/{booking_id}", json=body, headers=v["headers"])


def _send(v, booking_id):
    return client.post(f"/contracts/{booking_id}/send", headers=v["headers"])


def test_a_draft_saves_with_no_date_or_hours_yet():
    v = _setup_vendor()
    d = _draft(v)
    assert d["contract_status"] == "draft"
    assert d["date_iso"] == "TBD"
    assert d["time_start"] == "TBD" and d["time_end"] == "TBD"

    # Filled in later, and cleared again, by the editor's saves.
    r = _patch(v, d["booking_id"], date_iso="2027-12-04", time_start="18:00", time_end="23:00")
    assert r.status_code == 200, r.text
    assert r.json()["date_iso"] == "2027-12-04"
    r = _patch(v, d["booking_id"], date_iso="")
    assert r.status_code == 200 and r.json()["date_iso"] == "TBD"

    # What is typed still has to be a date.
    assert _patch(v, d["booking_id"], date_iso="next spring").status_code == 400


def test_a_draft_keeps_unfinished_lines_and_payments():
    v = _setup_vendor()
    d = _draft(v, line_items=[
        _package(v, unit_price_cents=120_000),
        {"kind": "custom", "name": "", "quantity": 1},
    ], payment_schedule=[
        {"label": "Deposit", "amount_cents": 10_000, "due_type": "on_signing"},
        {"label": "", "due_type": "before_event"},
    ])
    custom = d["line_items"][1]
    assert custom["name"] == "" and custom["unit_price_cents"] == 0
    assert len(d["payment_schedule"]) == 2


def test_a_draft_still_needs_one_of_the_vendors_packages():
    v = _setup_vendor()
    resp = client.post("/contracts", json={
        "draft": True, "date_iso": "", "time_start": "", "time_end": "",
        "line_items": [{"kind": "custom", "name": "Travel", "unit_price_cents": 5_000, "quantity": 1}],
        "conversation_id": new_chat(v["headers"]),
    }, headers=v["headers"])
    assert resp.status_code == 400
    assert "package" in resp.json()["detail"]


def test_saving_a_draft_again_and_again_keeps_one_version_and_no_edit_trail():
    v = _setup_vendor()
    d = _draft(v, date_iso="2027-12-05", time_start="18:00", time_end="23:00")
    for title in ("Sangeet", "Sangeet night", "Sangeet night — Priya & Dev"):
        r = _patch(v, d["booking_id"], document_title=title)
        assert r.status_code == 200, r.text
    got = client.get(f"/contracts/{d['booking_id']}", headers=v["headers"]).json()
    assert got["revision"] == d["revision"]
    assert got["document_title"] == "Sangeet night — Priya & Dev"
    kinds = [e["kind"] for e in got["timeline"]]
    assert "created" in kinds and "edited" not in kinds


def test_sending_checks_what_a_draft_left_unfinished():
    v = _setup_vendor()
    d = _draft(v, line_items=[_package(v, unit_price_cents=120_000), {"kind": "custom", "name": "", "quantity": 1}])
    bid = d["booking_id"]

    r = _send(v, bid)
    assert r.status_code == 400 and "date" in r.json()["detail"]
    _patch(v, bid, date_iso="2027-12-06")
    r = _send(v, bid)
    assert r.status_code == 400 and "start and end time" in r.json()["detail"]
    _patch(v, bid, time_start="18:00", time_end="23:00")
    r = _send(v, bid)
    assert r.status_code == 400 and "name" in r.json()["detail"]

    _patch(v, bid, line_items=[_package(v, unit_price_cents=120_000)], payment_schedule=[
        {"label": "Deposit", "amount_cents": 10_000, "due_type": "on_signing"},
    ])
    r = _send(v, bid)
    assert r.status_code == 400 and "add up" in r.json()["detail"]

    _patch(v, bid, payment_schedule=[{"label": "In full", "amount_cents": 120_000, "due_type": "on_signing"}])
    r = _send(v, bid)
    assert r.status_code == 200, r.text
    assert r.json()["contract_status"] == "sent"


def test_a_sent_contract_is_still_checked_on_every_edit():
    v = _setup_vendor()
    d = _draft(v, date_iso="2027-12-07", time_start="18:00", time_end="23:00", payment_schedule=[
        {"label": "In full", "amount_cents": 120_000, "due_type": "on_signing"},
    ])
    assert _send(v, d["booking_id"]).status_code == 200
    assert _patch(v, d["booking_id"], date_iso="").status_code == 400
    r = _patch(v, d["booking_id"], line_items=[_package(v, unit_price_cents=120_000), {"kind": "custom", "name": "", "quantity": 1}])
    assert r.status_code == 400
