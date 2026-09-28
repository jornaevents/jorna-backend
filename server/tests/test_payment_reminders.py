"""Payment reminders (docs/DECISIONS.md #18): the client before and on a
scheduled payment's due date, the vendor once it's overdue — each once, and
never for a payment already marked sent."""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.db.models import Booking
from app.services.payment_reminder_service import send_payment_reminders
from tests.test_api import TestingSessionLocal, client
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _created,
    _isolate,
    _setup_vendor,
)


@pytest.fixture
def outbox(monkeypatch):
    import app.services.payment_reminder_service as prs

    box = {"client": [], "vendor": []}
    monkeypatch.setattr(prs, "send_email", lambda **kw: box["client"].append(kw) or {"success": True})
    monkeypatch.setattr(prs, "notify_vendor_contract_event", lambda **kw: box["vendor"].append(kw) or {})
    return box


def _at(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc)


def _signed_contract(v, *, date_iso: str, schedule: list[dict]) -> dict:
    total = sum(i["amount_cents"] for i in schedule)
    resp = client.post("/contracts", json={
        "date_iso": date_iso, "time_start": "19:00", "time_end": "23:00",
        "line_items": [{"kind": "package", "service_id": v["service_id"], "unit_price_cents": total}],
        "payment_schedule": schedule, "guest_email": "priya@example.com", "guest_name": "Priya Mehta",
    }, headers=v["headers"])
    assert resp.status_code == 201, resp.text
    c = resp.json()
    _created["bookings"].append(c["booking_id"])
    signed = client.post(f"/guest-bookings/{c['contract_token']}/sign", json={"signer_name": "Priya Mehta"})
    assert signed.status_code == 200, signed.text
    return signed.json() | {"booking_id": c["booking_id"], "contract_token": c["contract_token"]}


def _sweep(when: date) -> int:
    db = TestingSessionLocal()
    try:
        return send_payment_reminders(db=db, now=_at(when))
    finally:
        db.close()


def test_the_client_is_reminded_before_and_on_the_day_then_the_vendor_once_overdue(outbox):
    v = _setup_vendor()
    c = _signed_contract(v, date_iso="2027-11-13", schedule=[
        {"label": "Deposit", "amount_cents": 50_000, "due_type": "on_signing"},
        {"label": "Second payment", "amount_cents": 50_000, "due_type": "date", "due_date": "2027-09-01"},
    ])
    client.post(f"/guest-bookings/{c['contract_token']}/mark-deposit-paid")

    assert _sweep(date(2027, 8, 28)) == 0  # four days out: too early
    assert _sweep(date(2027, 8, 29)) == 1
    assert "due on Wednesday, September 1" in outbox["client"][0]["subject"]
    assert f"booking-link?t={c['contract_token']}" in outbox["client"][0]["html"]
    assert _sweep(date(2027, 8, 30)) == 0  # already told

    assert _sweep(date(2027, 9, 1)) == 1
    assert "due today" in outbox["client"][1]["subject"]

    assert _sweep(date(2027, 9, 3)) == 0
    assert _sweep(date(2027, 9, 4)) == 1
    assert outbox["vendor"][0]["event"] == "contract_payment_overdue"
    assert "Priya Mehta's second payment is overdue" == outbox["vendor"][0]["title"]
    assert _sweep(date(2027, 9, 10)) == 0

    timeline = client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()["timeline"]
    reminders = [e["detail"]["reminder"] for e in timeline if e["kind"] == "payment_reminder"]
    assert reminders == ["upcoming", "due", "overdue"]


def test_nothing_is_sent_for_a_payment_marked_sent(outbox):
    v = _setup_vendor()
    c = _signed_contract(v, date_iso="2027-11-14", schedule=[
        {"label": "Payment in full", "amount_cents": 100_000, "due_type": "on_signing"},
    ])
    client.post(f"/guest-bookings/{c['contract_token']}/mark-paid")
    assert _sweep(date(2027, 11, 1)) == 0
    assert outbox == {"client": [], "vendor": []}


def test_a_date_that_passed_before_signing_counts_from_the_signature(outbox):
    """Signed ten days before the event, a "14 days before" balance's date is
    already gone — that's a payment due now, not one overdue."""
    v = _setup_vendor()
    today = datetime.now(timezone.utc).date()
    c = _signed_contract(v, date_iso=(today + timedelta(days=10)).isoformat(), schedule=[
        {"label": "Balance", "amount_cents": 100_000, "due_type": "before_event", "due_days": 14},
    ])
    assert _sweep(today) == 1
    assert "due today" in outbox["client"][0]["subject"]
    assert outbox["vendor"] == []


def test_a_catch_up_sweep_sends_only_the_latest_reminder(outbox):
    v = _setup_vendor()
    _signed_contract(v, date_iso="2027-11-15", schedule=[
        {"label": "Balance", "amount_cents": 100_000, "due_type": "date", "due_date": "2027-09-01"},
    ])
    assert _sweep(date(2027, 9, 10)) == 1
    assert outbox["client"] == []
    assert len(outbox["vendor"]) == 1


def test_voided_or_unsigned_contracts_owe_nothing(outbox):
    v = _setup_vendor()
    c = _signed_contract(v, date_iso="2027-11-16", schedule=[
        {"label": "Balance", "amount_cents": 100_000, "due_type": "date", "due_date": "2027-09-01"},
    ])
    db = TestingSessionLocal()
    db.query(Booking).filter(Booking.booking_id == c["booking_id"]).first().status = "rejected"
    db.commit()
    db.close()

    unsigned = client.post("/contracts", json={
        "date_iso": "2027-11-17", "time_start": "19:00", "time_end": "23:00",
        "line_items": [{"kind": "package", "service_id": v["service_id"], "unit_price_cents": 100_000}],
        "payment_schedule": [{"label": "Balance", "amount_cents": 100_000, "due_type": "date", "due_date": "2027-09-01"}],
        "guest_email": "priya@example.com",
    }, headers=v["headers"])
    _created["bookings"].append(unsigned.json()["booking_id"])

    assert _sweep(date(2027, 9, 1)) == 0
