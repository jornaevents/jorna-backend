"""Contracts as proposals (docs/DECISIONS.md #16): line items with add-ons
and a discount, a payment schedule that older readers still see as a
deposit, clause text, signing a specific revision into a frozen snapshot,
the timeline, account templates, and emailing the link.
"""

from app.db.models import Booking, ContractEvent, Lead, Service
from tests.test_api import TestingSessionLocal, client
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _created,
    _isolate,
    _setup_vendor,
)


def _with_addon(v) -> dict:
    db = TestingSessionLocal()
    service = db.query(Service).filter(Service.service_id == v["service_id"]).first()
    service.add_ons = [{"id": "xhour", "name": "Extra hour", "price": 250.0, "price_unit": "hour"}]
    db.commit()
    db.close()
    return v


SCHEDULE = [
    {"label": "Deposit", "amount_cents": 50_000, "due_type": "on_signing"},
    {"label": "Second payment", "amount_cents": 50_000, "due_type": "date", "due_date": "2027-09-01"},
    {"label": "Final balance", "amount_cents": 60_000, "due_type": "before_event", "due_days": 14},
]


def _proposal(v, **overrides):
    body = {
        "date_iso": "2027-11-13",
        "time_start": "19:00",
        "time_end": "23:00",
        "line_items": [
            {"kind": "package", "service_id": v["service_id"], "unit_price_cents": 140_000},
            {"kind": "addon", "service_id": v["service_id"], "addon_id": "xhour", "quantity": 2},
            {"kind": "custom", "name": "Uplighting", "unit_price_cents": 5_000, "quantity": 2},
        ],
        # 140,000 + 2 × 25,000 + 2 × 5,000 = 200,000, less 40,000.
        "discount_cents": 40_000,
        "payment_schedule": SCHEDULE,
        "terms_clauses": [
            {"key": "cancellation", "title": "Cancellation", "body": "The deposit is non-refundable."},
        ],
        "guest_name": "Priya Mehta",
    }
    body.update(overrides)
    resp = client.post("/contracts", json=body, headers=v["headers"])
    assert resp.status_code == 201, resp.text
    _created["bookings"].append(resp.json()["booking_id"])
    return resp.json()


def _sign(token: str, **extra):
    client.patch(f"/guest-bookings/{token}", json={"guest_email": "priya@example.com"})
    return client.post(f"/guest-bookings/{token}/sign", json={"signer_name": "Priya Mehta", **extra})


def test_line_items_add_ons_and_discount_total_on_the_server():
    v = _with_addon(_setup_vendor())
    c = _proposal(v)
    assert [i["name"] for i in c["line_items"]] == ["4-Hour Reception Package", "Extra hour", "Uplighting"]
    assert c["line_items"][1]["unit_price_cents"] == 25_000  # from the catalogue
    assert c["line_items"][1]["unit"] == "hour"
    assert c["subtotal_cents"] == 200_000
    assert c["amount_cents"] == 160_000
    assert c["service_id"] == v["service_id"]

    # Snapshotted: renaming the package later doesn't rewrite the contract.
    db = TestingSessionLocal()
    db.query(Service).filter(Service.service_id == v["service_id"]).first().name = "Renamed"
    db.commit()
    db.close()
    again = client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()
    assert again["line_items"][0]["name"] == "4-Hour Reception Package"


def test_a_document_that_doesnt_add_up_is_refused():
    v = _with_addon(_setup_vendor())
    other = _setup_vendor()
    base = {"date_iso": "2027-11-14", "time_start": "19:00", "time_end": "23:00"}

    short = client.post("/contracts", json={
        **base,
        "line_items": [{"kind": "package", "service_id": v["service_id"], "unit_price_cents": 100_000}],
        "payment_schedule": [{"label": "Deposit", "amount_cents": 20_000}],
    }, headers=v["headers"])
    assert short.status_code == 400
    assert "add up to $200.00 but the total is $1,000.00" in short.json()["detail"]

    theirs = client.post("/contracts", json={
        **base, "line_items": [{"kind": "package", "service_id": other["service_id"]}],
    }, headers=v["headers"])
    assert theirs.status_code == 400

    no_package = client.post("/contracts", json={
        **base, "line_items": [{"kind": "custom", "name": "Travel", "unit_price_cents": 5_000}],
    }, headers=v["headers"])
    assert no_package.status_code == 400
    assert "at least one of your packages" in no_package.json()["detail"]


def test_the_schedule_reads_as_a_deposit_to_older_readers_and_pays_down():
    v = _with_addon(_setup_vendor())
    c = _proposal(v)
    assert c["deposit_amount_cents"] == 50_000
    assert c["deposit_percent"] == 31  # 50,000 of 160,000
    due = {i["label"]: i["due_on"] for i in c["payment_schedule"]}
    assert due == {"Deposit": None, "Second payment": "2027-09-01", "Final balance": "2027-10-30"}

    token = c["contract_token"]
    assert _sign(token).status_code == 200
    deposit_id, second_id, final_id = (i["id"] for i in c["payment_schedule"])

    marked = client.post(f"/guest-bookings/{token}/payments/{deposit_id}/mark-paid")
    assert marked.status_code == 200, marked.text
    assert marked.json()["deposit_marked_paid_at"] is not None
    assert client.post(f"/guest-bookings/{token}/payments/{deposit_id}/mark-paid").status_code == 400

    # The Bookings page's existing "Confirm deposit" works on it.
    confirmed = client.post(f"/payments/bookings/{c['booking_id']}/confirm-deposit-received", headers=v["headers"])
    assert confirmed.status_code == 200, confirmed.text

    # A vendor can confirm a payment the client never marked.
    direct = client.post(
        f"/contracts/{c['booking_id']}/payments/{second_id}/confirm", headers=v["headers"],
    )
    assert direct.status_code == 200, direct.text
    assert direct.json()["payment_status"] == "unpaid"

    # "Paid in full" marks what's left, and then it reads as marked paid.
    client.post(f"/guest-bookings/{token}/mark-paid")
    row = client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()
    assert row["payment_status"] == "marked_paid"
    client.post(f"/payments/bookings/{c['booking_id']}/confirm-received", headers=v["headers"])
    row = client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()
    assert row["payment_status"] == "confirmed_paid"
    assert all(i["confirmed_at"] for i in row["payment_schedule"])
    assert final_id in {i["id"] for i in row["payment_schedule"]}


def test_a_signature_is_for_one_revision_and_freezes_what_it_said():
    v = _with_addon(_setup_vendor())
    c = _proposal(v)
    token = c["contract_token"]
    opened = client.get(f"/guest-bookings/{token}").json()
    assert opened["revision"] == 1
    assert opened["terms_clauses"][0]["title"] == "Cancellation"

    edited = client.patch(
        f"/contracts/{c['booking_id']}",
        json={"terms_clauses": [{"key": "cancellation", "title": "Cancellation", "body": "Refundable until June."}]},
        headers=v["headers"],
    )
    assert edited.json()["revision"] == 2

    stale = _sign(token, revision=1)
    assert stale.status_code == 409
    signed = _sign(token, revision=2)
    assert signed.status_code == 200, signed.text
    assert len(signed.json()["signed_snapshot_sha256"]) == 64

    db = TestingSessionLocal()
    snap = db.query(Booking).filter(Booking.booking_id == c["booking_id"]).first().signed_snapshot
    db.close()
    assert snap["terms_clauses"][0]["body"] == "Refundable until June."
    assert snap["amount_cents"] == 160_000 and snap["signer_name"] == "Priya Mehta"


def test_changing_the_total_needs_a_matching_schedule():
    v = _with_addon(_setup_vendor())
    c = _proposal(v)
    cheaper = client.patch(f"/contracts/{c['booking_id']}", json={"discount_cents": 60_000}, headers=v["headers"])
    assert cheaper.status_code == 400
    assert "payment schedule" in cheaper.json()["detail"]

    ok = client.patch(f"/contracts/{c['booking_id']}", json={
        "discount_cents": 60_000,
        "payment_schedule": [
            {"label": "Deposit", "amount_cents": 40_000},
            {"label": "Balance", "amount_cents": 100_000, "due_type": "before_event", "due_days": 7},
        ],
    }, headers=v["headers"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["amount_cents"] == 140_000
    # A bare total is ambiguous with several lines.
    assert client.patch(
        f"/contracts/{c['booking_id']}", json={"amount_cents": 1_000}, headers=v["headers"],
    ).status_code == 400


def test_the_old_one_package_form_still_works_as_a_one_line_contract():
    v = _setup_vendor()
    resp = client.post("/contracts", json={
        "service_id": v["service_id"], "date_iso": "2027-11-15", "time_start": "19:00",
        "time_end": "23:00", "amount_cents": 120_000, "deposit_percent": 25,
    }, headers=v["headers"])
    assert resp.status_code == 201, resp.text
    c = resp.json()
    _created["bookings"].append(c["booking_id"])
    assert c["payment_schedule"] is None
    assert c["deposit_amount_cents"] == 30_000
    assert len(c["line_items"]) == 1 and c["line_items"][0]["total_cents"] == 120_000

    moved = client.patch(f"/contracts/{c['booking_id']}", json={"amount_cents": 100_000}, headers=v["headers"]).json()
    assert moved["line_items"][0]["total_cents"] == 100_000
    assert moved["deposit_amount_cents"] == 25_000


def test_the_timeline_records_each_step_and_is_derived_for_older_contracts():
    v = _with_addon(_setup_vendor())
    c = _proposal(v)
    token = c["contract_token"]
    client.get(f"/guest-bookings/{token}")
    client.patch(f"/contracts/{c['booking_id']}", json={"guest_count": 150}, headers=v["headers"])
    _sign(token, revision=2)
    client.post(f"/guest-bookings/{token}/mark-deposit-paid")

    kinds = [e["kind"] for e in client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()["timeline"]]
    assert kinds == ["created", "sent", "viewed", "edited", "signed", "payment_marked"]

    # A contract from before events existed still gets a timeline.
    db = TestingSessionLocal()
    db.query(ContractEvent).filter(ContractEvent.booking_id == c["booking_id"]).delete()
    db.commit()
    db.close()
    derived = [e["kind"] for e in client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()["timeline"]]
    assert derived[:4] == ["created", "sent", "viewed", "signed"]


def test_templates_live_on_the_account_and_stay_private():
    v = _setup_vendor()
    other = _setup_vendor()
    body = {"clauses": [{"title": "Travel", "body": "Within 50 miles."}], "deposit_percent": 30}
    made = client.post("/contract-templates", json={"name": "Sangeet", "body": body}, headers=v["headers"])
    assert made.status_code == 201, made.text
    tid = made.json()["template_id"]

    assert client.get("/contract-templates", headers=v["headers"]).json()["items"][0]["body"] == body
    assert client.get("/contract-templates", headers=other["headers"]).json()["total"] == 0
    assert client.patch(f"/contract-templates/{tid}", json={"name": "x"}, headers=other["headers"]).status_code == 403

    renamed = client.patch(f"/contract-templates/{tid}", json={"name": "Sangeet night"}, headers=v["headers"])
    assert renamed.json()["name"] == "Sangeet night"
    assert client.delete(f"/contract-templates/{tid}", headers=v["headers"]).status_code == 200
    assert client.get("/contract-templates", headers=v["headers"]).json()["total"] == 0


def test_sending_can_email_the_client_their_link(monkeypatch):
    import app.services.contract_service as cs

    sent = []
    monkeypatch.setattr(cs, "send_email", lambda **kw: sent.append(kw) or {"success": True})
    v = _with_addon(_setup_vendor())

    no_email = client.post("/contracts", json={
        "date_iso": "2027-11-16", "time_start": "19:00", "time_end": "23:00",
        "line_items": [{"kind": "package", "service_id": v["service_id"]}], "email_client": True,
    }, headers=v["headers"])
    assert no_email.status_code == 400

    c = _proposal(v, date_iso="2027-11-17", guest_email="priya@example.com", email_client=True)
    assert sent[0]["to"] == "priya@example.com"
    assert f"booking-link?t={c['contract_token']}" in sent[0]["html"]

    client.post(f"/contracts/{c['booking_id']}/send", json={"email_client": True}, headers=v["headers"])
    assert len(sent) == 2
    kinds = [e["kind"] for e in client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()["timeline"]]
    assert kinds.count("emailed") == 2 and "resent" in kinds


def test_a_lead_converts_into_a_proposal_using_its_email(monkeypatch):
    import app.services.contract_service as cs

    sent = []
    monkeypatch.setattr(cs, "send_email", lambda **kw: sent.append(kw) or {"success": True})
    v = _with_addon(_setup_vendor())
    lead = client.post("/leads", json={"name": "Rohan Das", "email": "rohan@example.com"}, headers=v["headers"]).json()

    resp = client.post(f"/leads/{lead['lead_id']}/convert", json={
        "date_iso": "2027-11-18", "time_start": "19:00", "time_end": "23:00",
        "line_items": [{"kind": "package", "service_id": v["service_id"]}], "email_client": True,
    }, headers=v["headers"])
    assert resp.status_code == 201, resp.text
    _created["bookings"].append(resp.json()["booking_id"])
    assert resp.json()["guest_name"] == "Rohan Das"
    assert sent[0]["to"] == "rohan@example.com"

    db = TestingSessionLocal()
    assert db.query(Lead).filter(Lead.lead_id == lead["lead_id"]).first().status == "won"
    db.query(Lead).filter(Lead.lead_id == lead["lead_id"]).delete()
    db.commit()
    db.close()
