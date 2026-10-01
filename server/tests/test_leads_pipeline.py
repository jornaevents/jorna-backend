"""The vendor's leads pipeline (docs/DECISIONS.md #20): one list of everything
before a signed contract, archiving, "Add to leads" from a Messages thread,
and "Mark as unread".
"""

import uuid

import pytest

from app.db.models import Booking, Lead, Negotiation, User
from tests.test_api import TestingSessionLocal, client, make_auth_headers
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _create_contract,
    _created,
    _isolate,
    _setup_vendor,
)


def _client_user():
    uid = uuid.uuid4().hex[:8]
    db = TestingSessionLocal()
    u = User(
        email=f"pipe_c_{uid}@test.com", username=f"pipe_c_{uid}", password="pw",
        phone="7325550101", f_name="Meera", l_name="Shah", age=30, location="NJ",
        gender="F", language="EN", token_version=0,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    db.close()
    _created["users"].append(u.user_id)
    return u


def _request(v, who, **extra):
    db = TestingSessionLocal()
    fields = {"status": "pending", **extra}
    b = Booking(
        user_id=who.user_id, vendor_id=v["vendor_id"], service_id=v["service_id"],
        date_iso="2027-10-01", time_start="18:00", time_end="22:00", location="Pines Manor",
        payment_status="unpaid", payment_method="manual", **fields,
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    db.close()
    _created["bookings"].append(b.booking_id)
    return b


def _pipeline(v):
    resp = client.get("/leads/pipeline", headers=v["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


def _item(pipe, booking_id=None, lead_id=None):
    key = f"booking:{booking_id}" if booking_id else f"lead:{lead_id}"
    return next((i for i in pipe["items"] if i["id"] == key), None)


@pytest.fixture
def cleanup_leads():
    made: list[str] = []
    yield made
    db = TestingSessionLocal()
    db.query(Lead).filter(Lead.lead_id.in_(made)).delete(synchronize_session=False)
    db.commit()
    db.close()


def test_requests_drafts_and_sent_contracts_land_in_the_right_stage(cleanup_leads):
    v = _setup_vendor()
    couple = _client_user()
    request = _request(v, couple)
    draft = _create_contract(v, draft=True, date_iso="2027-11-20", guest_name="Draft Couple")
    sent = _create_contract(v, date_iso="2027-12-04", guest_name="Sent Couple")
    lead = client.post("/leads", json={"name": "Instagram Couple"}, headers=v["headers"]).json()
    cleanup_leads.append(lead["lead_id"])

    pipe = _pipeline(v)

    r = _item(pipe, request.booking_id)
    assert (r["source"], r["stage"], r["attention"], r["attention_reason"]) == (
        "request", "inquiry", "needs_you", "new_request",
    )
    assert r["name"] == "Meera Shah"
    assert r["estimated_value_cents"] == 140_000
    assert r["created_at"] is not None

    d = _item(pipe, draft["booking_id"])
    assert (d["stage"], d["attention_reason"]) == ("inquiry", "draft")

    s = _item(pipe, sent["booking_id"])
    assert (s["stage"], s["attention"], s["attention_reason"]) == ("negotiation", "waiting", "sent")
    assert s["name"] == "Sent Couple"

    lt = _item(pipe, lead_id=lead["lead_id"])
    assert (lt["source"], lt["stage"], lt["attention_reason"]) == ("lead", "inquiry", "new_lead")

    assert pipe["counts"]["inquiries"] == 3
    assert pipe["counts"]["negotiations"] == 1
    assert pipe["counts"]["needs_you"] == 3


def test_signed_voided_and_declined_requests_leave_the_list():
    v = _setup_vendor()
    signed = _create_contract(v, date_iso="2027-11-06", guest_email="a@example.com")
    client.patch(f"/guest-bookings/{signed['contract_token']}", json={"guest_email": "a@example.com"})
    assert client.post(
        f"/guest-bookings/{signed['contract_token']}/sign", json={"signer_name": "A B"},
    ).status_code == 200
    voided = _create_contract(v, date_iso="2027-11-27")
    assert client.post(f"/contracts/{voided['booking_id']}/void", headers=v["headers"]).status_code == 200
    declined = _request(v, _client_user(), rejected_reason="vendor_declined")
    db = TestingSessionLocal()
    db.query(Booking).filter(Booking.booking_id == declined.booking_id).update({"status": "rejected"})
    db.commit()
    db.close()

    ids = {i["id"] for i in _pipeline(v)["items"]}
    assert f"booking:{signed['booking_id']}" not in ids
    assert f"booking:{voided['booking_id']}" not in ids
    assert f"booking:{declined.booking_id}" not in ids


def test_a_couples_counter_offer_needs_the_vendor():
    v = _setup_vendor()
    couple = _client_user()
    b = _request(v, couple, status="negotiation_ongoing")
    db = TestingSessionLocal()
    neg = Negotiation(
        booking_id=b.booking_id, status="open", current_offer_cents=120_000,
        proposed_by=couple.user_id, created_at=b.created_at, updated_at=b.created_at,
    )
    db.add(neg)
    db.commit()
    db.close()

    item = _item(_pipeline(v), b.booking_id)
    assert (item["attention"], item["attention_reason"]) == ("needs_you", "counter_offer")

    db = TestingSessionLocal()
    db.query(Negotiation).filter(Negotiation.booking_id == b.booking_id).delete()
    db.commit()
    db.close()


def test_archiving_hides_without_declining_and_can_be_undone(cleanup_leads):
    v = _setup_vendor()
    b = _request(v, _client_user())
    resp = client.post(f"/bookings/{b.booking_id}/archive", json={"archived": True}, headers=v["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["vendor_archived_at"] is not None
    assert resp.json()["status"] == "pending"  # still a live request to its couple

    pipe = _pipeline(v)
    item = _item(pipe, b.booking_id)
    assert item["archived"] is True
    assert item["attention"] is None
    assert pipe["counts"]["archived"] == 1
    assert pipe["counts"]["needs_you"] == 0

    client.post(f"/bookings/{b.booking_id}/archive", json={"archived": False}, headers=v["headers"])
    assert _item(_pipeline(v), b.booking_id)["archived"] is False

    lead = client.post("/leads", json={"name": "Maybe Later"}, headers=v["headers"]).json()
    cleanup_leads.append(lead["lead_id"])
    patched = client.patch(f"/leads/{lead['lead_id']}", json={"archived": True}, headers=v["headers"]).json()
    assert patched["archived_at"] is not None
    assert patched["status"] == "new"


def test_only_the_vendor_can_archive_and_never_a_signed_booking():
    v = _setup_vendor()
    other = _setup_vendor()
    b = _request(v, _client_user())
    assert client.post(f"/bookings/{b.booking_id}/archive", headers=other["headers"]).status_code == 404

    signed = _create_contract(v, date_iso="2027-11-06")
    client.patch(f"/guest-bookings/{signed['contract_token']}", json={"guest_email": "s@example.com"})
    client.post(f"/guest-bookings/{signed['contract_token']}/sign", json={"signer_name": "S T"})
    assert client.post(f"/bookings/{signed['booking_id']}/archive", headers=v["headers"]).status_code == 400


def _enquiry(v, couple):
    resp = client.post(
        "/conversations/enquiry",
        json={"vendor_id": v["vendor_id"], "content": "Are you free on the 14th?"},
        headers=make_auth_headers(couple),
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["conversation_id"]


def test_add_to_leads_from_a_conversation_is_idempotent(cleanup_leads):
    v = _setup_vendor()
    couple = _client_user()
    conv_id = _enquiry(v, couple)

    first = client.post(f"/conversations/{conv_id}/lead", headers=v["headers"])
    assert first.status_code == 201, first.text
    lead = first.json()
    cleanup_leads.append(lead["lead_id"])
    assert lead["name"] == "Meera Shah"
    assert lead["conversation_id"] == conv_id
    assert lead["user_id"] == couple.user_id

    again = client.post(f"/conversations/{conv_id}/lead", headers=v["headers"])
    assert again.status_code == 200
    assert again.json()["lead_id"] == lead["lead_id"]

    item = _item(_pipeline(v), lead_id=lead["lead_id"])
    assert item["conversation_id"] == conv_id

    # Not a member of the thread: no lead.
    stranger = _setup_vendor()
    assert client.post(f"/conversations/{conv_id}/lead", headers=stranger["headers"]).status_code == 404


def test_mark_as_unread_counts_until_the_thread_is_opened():
    v = _setup_vendor()
    couple = _client_user()
    conv_id = _enquiry(v, couple)
    h = v["headers"]

    client.get(f"/conversations/{conv_id}/messages", headers=h)  # read it
    assert client.get("/conversations/unread-count", headers=h).json()["unread_count"] == 0

    marked = client.post(f"/conversations/{conv_id}/unread", headers=h)
    assert marked.status_code == 200, marked.text
    assert marked.json()["unread_count"] == 1
    assert client.get("/conversations/unread-count", headers=h).json()["unread_count"] == 1
    listed = next(c for c in client.get("/conversations", headers=h).json() if c["conversation_id"] == conv_id)
    assert listed["unread_count"] == 1

    client.get(f"/conversations/{conv_id}/messages", headers=h)  # open it again
    assert client.get("/conversations/unread-count", headers=h).json()["unread_count"] == 0

    stranger = _setup_vendor()
    assert client.post(f"/conversations/{conv_id}/unread", headers=stranger["headers"]).status_code == 404


def test_new_bookings_record_when_they_were_made():
    v = _setup_vendor()
    contract = _create_contract(v, date_iso="2027-12-18")
    booking = client.get(f"/bookings/vendor/{v['vendor_id']}", headers=v["headers"]).json()["items"]
    mine = next(b for b in booking if b["booking_id"] == contract["booking_id"])
    assert mine["created_at"] is not None
    assert mine["sent_at"] is not None
