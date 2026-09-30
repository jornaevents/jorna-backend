"""CRUD + ownership + convert-to-contract for informal, off-platform Leads.
See docs/DECISIONS.md #13 and app/services/contract_service.py.
"""

import uuid

import pytest

from app.db.models import Booking, Lead, Service, User, Vendor
from tests.test_api import TestingSessionLocal, client, make_auth_headers

_created = {"users": [], "vendors": [], "services": [], "bookings": [], "leads": []}


@pytest.fixture(autouse=True)
def _isolate():
    """Cleanup so other files' search/listing assertions (which assume a
    known, small dataset) don't see the vendors/services this file creates.
    See test_guest_booking_flow.py's identical fixture."""
    yield
    db = TestingSessionLocal()
    if _created["leads"]:
        db.query(Lead).filter(Lead.lead_id.in_(_created["leads"])).delete(synchronize_session=False)
    if _created["bookings"]:
        db.query(Booking).filter(Booking.booking_id.in_(_created["bookings"])).delete(synchronize_session=False)
    if _created["services"]:
        db.query(Service).filter(Service.service_id.in_(_created["services"])).delete(synchronize_session=False)
    if _created["vendors"]:
        db.query(Vendor).filter(Vendor.vendor_id.in_(_created["vendors"])).delete(synchronize_session=False)
    if _created["users"]:
        db.query(User).filter(User.user_id.in_(_created["users"])).delete(synchronize_session=False)
    db.commit()
    db.close()
    for v in _created.values():
        v.clear()


def _setup_vendor(prefix="lead"):
    uid = uuid.uuid4().hex[:8]
    db = TestingSessionLocal()
    vendor_user = User(
        email=f"{prefix}_v_{uid}@test.com", username=f"{prefix}_v_{uid}", password="pw",
        phone="1", f_name="V", l_name="N", age=30, location="NJ",
        gender="F", language="EN", token_version=0,
    )
    db.add(vendor_user); db.commit(); db.refresh(vendor_user)
    vendor = Vendor(
        user_id=vendor_user.user_id, bio="b", category="venue", rating=0.0, num_events=0,
        payment_method="manual",
    )
    db.add(vendor); db.commit(); db.refresh(vendor)
    service = Service(
        name="svc", price=1000.0, price_unit="event", vendor_id=vendor.vendor_id,
        experience="e", category="venue", negotiable=False,
    )
    db.add(service); db.commit(); db.refresh(service)
    headers = make_auth_headers(vendor_user)
    result = {"service_id": service.service_id, "headers": headers}
    _created["users"].append(vendor_user.user_id)
    _created["vendors"].append(vendor.vendor_id)
    _created["services"].append(service.service_id)
    db.close()
    return result


def test_create_list_and_update_a_lead():
    v = _setup_vendor()
    created = client.post(
        "/leads",
        json={"name": "Iyer Family", "note": "Reached out on Instagram. 200+ guests."},
        headers=v["headers"],
    )
    assert created.status_code == 201, created.text
    lead_id = created.json()["lead_id"]
    _created["leads"].append(lead_id)
    assert created.json()["status"] == "new"

    listed = client.get("/leads", headers=v["headers"])
    assert listed.status_code == 200
    assert any(l["lead_id"] == lead_id for l in listed.json()["items"])

    updated = client.patch(f"/leads/{lead_id}", json={"status": "contacted"}, headers=v["headers"])
    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == "contacted"


def test_a_vendor_cannot_see_or_edit_another_vendors_lead():
    v1 = _setup_vendor("lead1")
    v2 = _setup_vendor("lead2")
    created = client.post("/leads", json={"name": "Someone"}, headers=v1["headers"])
    lead_id = created.json()["lead_id"]
    _created["leads"].append(lead_id)

    resp = client.patch(f"/leads/{lead_id}", json={"status": "won"}, headers=v2["headers"])
    assert resp.status_code == 403, resp.text


def test_delete_a_lead():
    v = _setup_vendor()
    created = client.post("/leads", json={"name": "Reddy Family"}, headers=v["headers"])
    lead_id = created.json()["lead_id"]

    deleted = client.delete(f"/leads/{lead_id}", headers=v["headers"])
    assert deleted.status_code == 200, deleted.text

    listed = client.get("/leads", headers=v["headers"])
    assert not any(l["lead_id"] == lead_id for l in listed.json()["items"])


def test_convert_a_lead_into_a_real_contract():
    v = _setup_vendor()
    created = client.post(
        "/leads", json={"name": "Reddy Family", "phone": "5551234", "email": "reddy@example.com"},
        headers=v["headers"],
    )
    lead_id = created.json()["lead_id"]
    _created["leads"].append(lead_id)

    converted = client.post(
        f"/leads/{lead_id}/convert",
        json={
            "service_id": v["service_id"], "date_iso": "2027-04-03",
            "time_start": "18:00", "time_end": "22:00", "amount_cents": 220_000,
        },
        headers=v["headers"],
    )
    assert converted.status_code == 201, converted.text
    _created["bookings"].append(converted.json()["booking_id"])
    assert converted.json()["guest_name"] == "Reddy Family"
    assert converted.json()["guest_phone"] == "5551234"

    lead_after = client.get("/leads", headers=v["headers"]).json()["items"]
    lead = next(l for l in lead_after if l["lead_id"] == lead_id)
    assert lead["status"] == "won"
    assert lead["converted_booking_id"] == converted.json()["booking_id"]

    again = client.post(
        f"/leads/{lead_id}/convert",
        json={
            "service_id": v["service_id"], "date_iso": "2027-04-03",
            "time_start": "18:00", "time_end": "22:00", "amount_cents": 220_000,
        },
        headers=v["headers"],
    )
    assert again.status_code == 400, again.text
