"""Guards and lead states from the Oct 5 walkthrough (docs/DECISIONS.md #28):
payments can't be scheduled for a day that's gone, a hold ends when the event
starts, leads' details are checked, a vendor can say why they declined, and
closed leads are archived with how they ended."""

from datetime import date, datetime, timedelta, timezone

from app.db.models import Booking, Bundle, Lead
from app.services.contract_service import _default_schedule
from tests.test_api import TestingSessionLocal, client, make_auth_headers
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _create_contract,
    _created,
    _isolate,
    _setup_vendor,
)
from tests.test_leads_pipeline import _client_user, _item, _pipeline, _request


def _days(n: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=n)).isoformat()


def _schedule_contract(v, date_iso, schedule, **extra):
    total = sum(i["amount_cents"] for i in schedule)
    return client.post("/contracts", json={
        "date_iso": date_iso, "time_start": "18:00", "time_end": "22:00",
        "line_items": [{"kind": "package", "service_id": v["service_id"], "unit_price_cents": total}],
        "payment_schedule": schedule, **extra,
    }, headers=v["headers"])


# ── Payment dates ──────────────────────────────────────────────────────

def test_a_payment_counted_back_past_today_is_refused():
    v = _setup_vendor()
    resp = _schedule_contract(v, _days(250), [
        {"label": "Deposit", "amount_cents": 50_000, "due_type": "on_signing"},
        {"label": "Balance", "amount_cents": 50_000, "due_type": "before_event", "due_days": 400},
    ])
    assert resp.status_code == 400
    assert "“Balance” would be due on" in resp.json()["detail"]


def test_a_payment_on_a_past_date_is_refused():
    v = _setup_vendor()
    resp = _schedule_contract(v, _days(60), [
        {"label": "Payment in full", "amount_cents": 100_000, "due_type": "date", "due_date": _days(-10)},
    ])
    assert resp.status_code == 400
    assert "already passed" in resp.json()["detail"]


def test_a_payment_after_the_event_is_allowed():
    v = _setup_vendor()
    resp = _schedule_contract(v, _days(60), [
        {"label": "Deposit", "amount_cents": 50_000, "due_type": "on_signing"},
        {"label": "On delivery", "amount_cents": 50_000, "due_type": "date", "due_date": _days(90)},
    ])
    assert resp.status_code == 201, resp.text
    _created["bookings"].append(resp.json()["booking_id"])


def test_editing_the_schedule_is_checked_but_moving_the_date_alone_is_not():
    v = _setup_vendor()
    resp = _schedule_contract(v, _days(60), [
        {"label": "Deposit", "amount_cents": 50_000, "due_type": "on_signing"},
        {"label": "Balance", "amount_cents": 50_000, "due_type": "before_event", "due_days": 30},
    ])
    c = resp.json()
    _created["bookings"].append(c["booking_id"])

    bad = client.patch(f"/contracts/{c['booking_id']}", json={"payment_schedule": [
        {"label": "Deposit", "amount_cents": 50_000, "due_type": "on_signing"},
        {"label": "Balance", "amount_cents": 50_000, "due_type": "before_event", "due_days": 90},
    ]}, headers=v["headers"])
    assert bad.status_code == 400

    moved = client.patch(f"/contracts/{c['booking_id']}", json={"date_iso": _days(20)}, headers=v["headers"])
    assert moved.status_code == 200, moved.text


def test_sending_a_stale_draft_is_refused_but_a_resend_is_not():
    v = _setup_vendor()
    resp = _schedule_contract(v, _days(60), [
        {"label": "Payment in full", "amount_cents": 100_000, "due_type": "date", "due_date": _days(5)},
    ], draft=True)
    c = resp.json()
    _created["bookings"].append(c["booking_id"])
    db = TestingSessionLocal()
    b = db.query(Booking).filter(Booking.booking_id == c["booking_id"]).first()
    b.payment_schedule = [{**b.payment_schedule[0], "due_date": _days(-5)}]
    db.commit()
    db.close()

    send = client.post(f"/contracts/{c['booking_id']}/send", json={}, headers=v["headers"])
    assert send.status_code == 400

    db = TestingSessionLocal()
    db.query(Booking).filter(Booking.booking_id == c["booking_id"]).update({"sent_at": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)})
    db.commit()
    db.close()
    resend = client.post(f"/contracts/{c['booking_id']}/send", json={}, headers=v["headers"])
    assert resend.status_code == 200, resend.text


def test_the_usual_schedule_never_counts_back_past_today():
    soon = _days(5)
    schedule = _default_schedule(100_000, 50, {"preset": "three", "balance_days_before": 14}, date_iso=soon)
    assert [i["due_days"] for i in schedule if i["due_type"] == "before_event"] == [5, 5]
    far = _default_schedule(100_000, 50, None, date_iso=_days(200))
    assert far[1]["due_days"] == 14


# ── Holds ──────────────────────────────────────────────────────────────

def test_a_hold_ends_when_the_event_starts():
    v = _setup_vendor()
    event = _days(3)
    c = _create_contract(v, date_iso=event, hold_days=30)
    held = datetime.fromisoformat(c["hold_expires_at"].replace("Z", "+00:00")).replace(tzinfo=None)
    assert held <= datetime.combine(date.fromisoformat(event), datetime.min.time())


# ── Leads ──────────────────────────────────────────────────────────────

def test_a_lead_with_a_bad_email_phone_or_past_date_is_refused():
    v = _setup_vendor()
    for body, word in (
        ({"name": "Neha", "email": "neha@"}, "email"),
        ({"name": "Neha", "phone": "call me maybe"}, "phone"),
        ({"name": "Neha", "event_date_iso": "2025-02-01"}, "passed"),
    ):
        resp = client.post("/leads", json=body, headers=v["headers"])
        assert resp.status_code == 422, body
        assert word in resp.json()["detail"]

    ok = client.post("/leads", json={
        "name": "Neha", "email": " neha@example.com ", "phone": "+1 (732) 555-0101", "event_date_iso": _days(100),
    }, headers=v["headers"])
    assert ok.status_code in (200, 201), ok.text
    lead = ok.json()
    assert lead["email"] == "neha@example.com"
    bad = client.patch(f"/leads/{lead['lead_id']}", json={"email": "nope"}, headers=v["headers"])
    assert bad.status_code == 422
    _cleanup_lead(lead["lead_id"])


def _cleanup_lead(lead_id):
    db = TestingSessionLocal()
    db.query(Lead).filter(Lead.lead_id == lead_id).delete()
    db.commit()
    db.close()


# ── Declining, and closed leads ────────────────────────────────────────

def test_a_vendor_can_say_why_they_declined_and_the_client_is_told(monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.email_service.send_email", lambda **kw: sent.append(kw) or {"success": True})
    v = _setup_vendor()
    couple = _client_user()
    b = _request(v, couple)

    resp = client.put(
        f"/bookings/{b.booking_id}/status",
        json={"status": "rejected", "note": "Booked that weekend, sorry!"},
        headers=v["headers"],
    )
    assert resp.status_code == 200, resp.text
    told = [m for m in sent if "can't take" in m["subject"]]
    assert len(told) == 1 and told[0]["to"] == couple.email
    assert "Booked that weekend, sorry!" in told[0]["html"]

    item = _item(_pipeline(v), b.booking_id)
    assert (item["archived"], item["outcome"], item["outcome_note"]) == (True, "declined", "Booked that weekend, sorry!")


def test_a_declined_contract_and_a_lost_lead_are_archived_with_their_outcome():
    v = _setup_vendor()
    c = _create_contract(v, date_iso=_days(120))
    assert client.post(f"/guest-bookings/{c['contract_token']}/decline", json={"reason": "Went with family"}).status_code == 200
    lead = client.post("/leads", json={"name": "Ravi"}, headers=v["headers"]).json()
    client.patch(f"/leads/{lead['lead_id']}", json={"status": "lost"}, headers=v["headers"])

    pipe = _pipeline(v)
    declined = _item(pipe, c["booking_id"])
    assert (declined["archived"], declined["outcome"], declined["outcome_note"]) == (True, "contract_declined", "Went with family")
    lost = _item(pipe, lead_id=lead["lead_id"])
    assert (lost["archived"], lost["outcome"]) == (True, "not_going_ahead")
    assert pipe["counts"]["archived"] >= 2
    _cleanup_lead(lead["lead_id"])


def test_a_client_cancelling_a_sent_request_keeps_it_for_both_sides():
    v = _setup_vendor()
    couple = _client_user()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db = TestingSessionLocal()
    bundle = Bundle(user_id=couple.user_id, name="Sangeet", status="confirmed", created_at=now, updated_at=now)
    db.add(bundle)
    db.commit()
    bundle_id = bundle.bundle_id
    db.close()
    b = _request(v, couple, bundle_id=bundle_id)
    headers = make_auth_headers(couple)

    resp = client.delete(f"/bundles/{bundle_id}/bookings/{b.booking_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    kept = next(x for x in resp.json()["bookings"] if x["booking_id"] == b.booking_id)
    assert (kept["status"], kept["rejected_reason"]) == ("rejected", "client_cancelled")

    item = _item(_pipeline(v), b.booking_id)
    assert (item["archived"], item["outcome"]) == (True, "cancelled_by_client")

    # Removing it again clears it from the plan for good.
    again = client.delete(f"/bundles/{bundle_id}/bookings/{b.booking_id}", headers=headers)
    assert again.status_code == 200, again.text
    assert all(x["booking_id"] != b.booking_id for x in again.json()["bookings"])
    _created["bookings"].remove(b.booking_id)
    db = TestingSessionLocal()
    db.query(Bundle).filter(Bundle.bundle_id == bundle_id).delete()
    db.commit()
    db.close()
