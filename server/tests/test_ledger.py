"""The earnings ledger (docs/DECISIONS.md #33): one row per payment on a
vendor's signed bookings, its status, the year filter and totals, how the
client paid, "Remind client", and undoing one step at a time."""

import uuid
from datetime import date, datetime, timezone

import pytest

from app.db.models import Booking, User
from app.services import contract_document, ledger_service
from tests.test_api import TestingSessionLocal, client, make_auth_headers
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _created,
    _isolate,
    _setup_vendor,
)
from tests.test_payment_reminders import _signed_contract

SCHEDULE = [
    {"label": "Deposit", "amount_cents": 50_000, "due_type": "on_signing"},
    {"label": "Second payment", "amount_cents": 30_000, "due_type": "date", "due_date": "2027-09-01"},
    {"label": "Balance", "amount_cents": 20_000, "due_type": "before_event", "due_days": 7},
]


@pytest.fixture
def clock(monkeypatch):
    """Move the ledger's "today", and the time events are recorded at, to a
    chosen day — signing still happens today, for real."""
    def set_day(day: date):
        monkeypatch.setattr(ledger_service, "_today", lambda: day)
        monkeypatch.setattr(contract_document, "_now", lambda: datetime(day.year, day.month, day.day, 15))
    return set_day


@pytest.fixture
def outbox(monkeypatch):
    import app.services.payment_reminder_service as prs

    sent = []
    capture = lambda **kw: sent.append(kw) or {"success": True}  # noqa: E731
    monkeypatch.setattr(prs, "send_email", capture)
    monkeypatch.setattr(ledger_service, "send_email", capture)
    return sent


def _contract(v):
    c = _signed_contract(v, date_iso="2027-11-13", schedule=SCHEDULE)
    ids = [i["id"] for i in c["payment_schedule"]]
    return c, ids


def _ledger(v, year=None):
    resp = client.get("/vendors/me/ledger" + (f"?year={year}" if year else ""), headers=v["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


def _row(ledger, installment_id):
    return next(r for r in ledger["rows"] + ledger["other_rows"] if r["installment_id"] == installment_id)


def _mark(c, installment_id, paid_via=None):
    body = {"paid_via": paid_via} if paid_via else None
    return client.post(f"/guest-bookings/{c['contract_token']}/payments/{installment_id}/mark-paid", json=body)


def _confirm(v, c, installment_id):
    resp = client.post(f"/contracts/{c['booking_id']}/payments/{installment_id}/confirm", headers=v["headers"])
    assert resp.status_code == 200, resp.text


def _booking(booking_id) -> Booking:
    db = TestingSessionLocal()
    try:
        return db.query(Booking).filter(Booking.booking_id == booking_id).first()
    finally:
        db.close()


def test_rows_statuses_and_totals(clock):
    v = _setup_vendor()
    c, (deposit, second, balance) = _contract(v)
    assert _mark(c, deposit, "zelle").status_code == 200
    clock(date(2027, 8, 28))

    ledger = _ledger(v)
    rows = {r["installment_id"]: r for r in ledger["rows"]}
    assert [rows[i]["status"] for i in (deposit, second, balance)] == ["client_says_sent", "due_soon", "scheduled"]
    assert rows[deposit]["paid_via"] == "zelle" and rows[deposit]["marked_by_client"] is True
    assert (rows[second]["sequence"], rows[second]["count"], rows[balance]["due_on"]) == (2, 3, "2027-11-06")
    info = rows[deposit]["booking"]
    assert (info["client_name"], info["state"], info["signed_by"], info["has_contract"]) == ("Priya Mehta", "upcoming", "Priya Mehta", True)
    assert ledger["totals"] == {
        "received_cents": 0, "received_count": 0,
        "to_come_cents": 50_000, "to_come_count": 2,
        "overdue_cents": 0, "overdue_count": 0,
        "waiting_cents": 50_000, "waiting_count": 1,
    }
    assert ledger["payment_methods"] == {"venmo": "@arjun-dj", "zelle": "arjun@example.com"}

    _confirm(v, c, deposit)
    assert _row(_ledger(v), deposit)["status"] == "received"


def test_a_year_keeps_the_payments_due_in_it_and_the_rest_of_their_schedules(clock):
    v = _setup_vendor()
    _, (deposit, second, balance) = _contract(v)
    signed_year = datetime.now(timezone.utc).year
    clock(date(2027, 8, 28))

    later = _ledger(v, 2027)
    assert {r["installment_id"] for r in later["rows"]} == {second, balance}
    assert [r["installment_id"] for r in later["other_rows"]] == [deposit]
    assert later["totals"]["to_come_count"] == 2

    signing = _ledger(v, signed_year)
    assert [r["installment_id"] for r in signing["rows"]] == [deposit]
    assert _ledger(v, 2031)["rows"] == []


def test_an_overdue_payment_can_be_reminded_once_a_day(clock, outbox):
    v = _setup_vendor()
    c, (deposit, second, balance) = _contract(v)
    _confirm(v, c, deposit)
    clock(date(2027, 9, 5))

    row = _row(_ledger(v), second)
    assert (row["status"], row["can_remind"]) == ("overdue", True)
    sent = client.post(f"/contracts/{c['booking_id']}/payments/{second}/remind", headers=v["headers"])
    assert sent.status_code == 200, sent.text
    assert sent.json()["can_remind"] is False and sent.json()["reminded_at"].startswith("2027-09-05")
    [email] = outbox
    assert email["to"] == "priya@example.com"
    assert "second payment is overdue" in email["subject"]
    assert "Venmo @arjun-dj" in email["html"] and "Zelle arjun@example.com" in email["html"]

    again = client.post(f"/contracts/{c['booking_id']}/payments/{second}/remind", headers=v["headers"])
    assert again.status_code == 429
    clock(date(2027, 9, 6))
    assert _row(_ledger(v), second)["can_remind"] is True

    not_due = client.post(f"/contracts/{c['booking_id']}/payments/{balance}/remind", headers=v["headers"])
    assert not_due.status_code == 400 and "overdue" in not_due.json()["detail"]
    events = [e for e in _row(_ledger(v), second)["events"] if e["kind"] == "payment_reminder"]
    assert events[-1]["reminder"] == "manual"


def test_undo_takes_back_one_step_at_a_time(clock, outbox):
    v = _setup_vendor()
    c, (deposit, second, balance) = _contract(v)
    clock(date(2027, 8, 28))
    undo = lambda iid: client.post(f"/contracts/{c['booking_id']}/payments/{iid}/not-received", headers=v["headers"])  # noqa: E731

    # The client says sent, the vendor hasn't got it: the mark goes, and the client is told.
    _mark(c, second, "venmo")
    r = undo(second)
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["paid_via"], r.json()["marked_by_client"]) == ("due_soon", None, False)
    [email] = outbox
    assert email["subject"] == "Arjun Kapoor hasn't received your second payment yet"

    # Marked again and confirmed, then the confirmation is taken back: the client's mark stays.
    _mark(c, second, "zelle")
    _confirm(v, c, second)
    r = undo(second)
    assert (r.json()["status"], r.json()["paid_via"]) == ("client_says_sent", "zelle")
    assert len(outbox) == 1  # taking back your own confirmation emails nobody

    # Confirmed with no mark from the client: undoing it clears both.
    _confirm(v, c, balance)
    assert undo(balance).json()["status"] == "scheduled"

    nothing = undo(balance)
    assert nothing.status_code == 400 and "nothing to undo" in nothing.json()["detail"]

    # The booking-level fields follow the schedule.
    _confirm(v, c, deposit)
    _confirm(v, c, second)
    _confirm(v, c, balance)
    assert _booking(c["booking_id"]).payment_status == "confirmed_paid"
    undo(balance)
    assert _booking(c["booking_id"]).payment_status == "unpaid"


def test_a_cancelled_booking_keeps_what_was_received(clock):
    v = _setup_vendor()
    c, (deposit, second, balance) = _contract(v)
    _confirm(v, c, deposit)
    _mark(c, second)
    db = TestingSessionLocal()
    db.query(Booking).filter(Booking.booking_id == c["booking_id"]).update({"cancelled_at": datetime(2026, 12, 1)})
    db.commit(); db.close()
    clock(date(2027, 9, 5))

    ledger = _ledger(v)
    statuses = [_row(ledger, i)["status"] for i in (deposit, second, balance)]
    assert statuses == ["received", "client_says_sent", "cancelled"]
    assert _row(ledger, balance)["booking"]["state"] == "cancelled"
    assert ledger["totals"]["overdue_count"] == 0
    refused = client.post(f"/contracts/{c['booking_id']}/payments/{second}/not-received", headers=v["headers"])
    assert refused.status_code == 400


def test_a_booking_from_before_contracts_is_one_full_amount_row():
    v = _setup_vendor()
    db = TestingSessionLocal()
    old = Booking(
        user_id=None, vendor_id=_created["vendors"][-1], service_id=v["service_id"], time_start="18:00",
        time_end="22:00", location="Montclair, NJ", date_iso="2025-08-16", status="approved",
        payment_status="confirmed_paid", amount_cents=160_000, confirmed_at=datetime(2025, 7, 1),
        manual_payment_confirmed_at=datetime(2025, 8, 9), guest_name="Kavya Menon",
    )
    db.add(old); db.commit(); db.refresh(old)
    _created["bookings"].append(old.booking_id)
    db.close()

    [row] = [r for r in _ledger(v)["rows"] if r["booking_id"] == old.booking_id]
    assert (row["label"], row["status"], row["installment_id"], row["can_undo"]) == ("Full amount", "received", None, False)
    assert row["booking"]["has_contract"] is False and row["booking"]["state"] == "done"


def test_unsigned_voided_and_other_vendors_bookings_stay_out():
    v = _setup_vendor()
    unsigned = client.post("/contracts", json={
        "date_iso": "2027-10-10", "time_start": "19:00", "time_end": "23:00",
        "line_items": [{"kind": "package", "service_id": v["service_id"], "unit_price_cents": 100_000}],
        "guest_email": "x@example.com", "guest_name": "X",
    }, headers=v["headers"]).json()
    _created["bookings"].append(unsigned["booking_id"])
    c, _ = _contract(v)
    db = TestingSessionLocal()
    db.query(Booking).filter(Booking.booking_id == c["booking_id"]).update({"voided_at": datetime(2026, 12, 1)})
    db.commit(); db.close()
    assert _ledger(v)["rows"] == []

    other = _setup_vendor()
    c2, (d2, _, _) = _contract(other)
    assert all(r["booking_id"] != c2["booking_id"] for r in _ledger(v)["rows"])
    stranger = client.post(f"/contracts/{c2['booking_id']}/payments/{d2}/not-received", headers=v["headers"])
    assert stranger.status_code == 404


def test_paid_via_must_be_venmo_zelle_or_other():
    v = _setup_vendor()
    c, (deposit, _, _) = _contract(v)
    bad = _mark(c, deposit, "cash app")
    assert bad.status_code == 422
    assert _mark(c, deposit, "other").status_code == 200


def test_a_non_vendor_has_no_ledger():
    db = TestingSessionLocal()
    uid = uuid.uuid4().hex[:8]
    u = User(email=f"led_{uid}@test.com", username=f"led_{uid}", password="pw", phone="1", f_name="C", l_name="L",
             age=30, location="NJ", gender="F", language="EN", token_version=0)
    db.add(u); db.commit(); db.refresh(u)
    _created["users"].append(u.user_id)
    headers = make_auth_headers(u)
    db.close()
    assert client.get("/vendors/me/ledger", headers=headers).status_code == 403
