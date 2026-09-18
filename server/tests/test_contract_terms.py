"""Editing a contract's terms before it's sent/signed, and the immutability
guard once it has been. See app/services/contract_service.py.
"""

import uuid

import pytest

from app.db.models import Booking, Service, User, Vendor
from app.limiter import limiter
from tests.test_api import TestingSessionLocal, client, make_auth_headers

_created = {"users": [], "vendors": [], "services": [], "bookings": []}


@pytest.fixture(autouse=True)
def _isolate():
    """See test_guest_booking_flow.py's identical fixture."""
    limiter.enabled = False
    yield
    limiter.enabled = True
    db = TestingSessionLocal()
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


def _setup_vendor():
    uid = uuid.uuid4().hex[:8]
    db = TestingSessionLocal()
    vendor_user = User(
        email=f"ct_v_{uid}@test.com", username=f"ct_v_{uid}", password="pw",
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


def _create(v, **overrides):
    body = {
        "service_id": v["service_id"], "date_iso": "2027-09-01",
        "time_start": "10:00", "time_end": "14:00", "amount_cents": 100_000,
        "deposit_percent": 25,
    }
    body.update(overrides)
    resp = client.post("/contracts", json=body, headers=v["headers"])
    assert resp.status_code == 201, resp.text
    _created["bookings"].append(resp.json()["booking_id"])
    return resp.json()


def test_editing_terms_recomputes_the_deposit_snapshot():
    v = _setup_vendor()
    contract = _create(v)
    assert contract["deposit_amount_cents"] == 25_000

    edited = client.patch(
        f"/contracts/{contract['booking_id']}",
        json={"amount_cents": 200_000},
        headers=v["headers"],
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["deposit_amount_cents"] == 50_000


def test_cannot_edit_a_signed_contract():
    v = _setup_vendor()
    contract = _create(v)
    token = contract["contract_token"]
    client.patch(f"/guest-bookings/{token}", json={"guest_email": "g@example.com"})
    client.post(f"/guest-bookings/{token}/sign", json={"signer_name": "Guest"})

    edited = client.patch(
        f"/contracts/{contract['booking_id']}",
        json={"amount_cents": 999_999},
        headers=v["headers"],
    )
    assert edited.status_code == 400, edited.text


def test_a_vendor_cannot_view_or_edit_another_vendors_contract():
    v1 = _setup_vendor()
    v2 = _setup_vendor()
    contract = _create(v1)

    resp = client.get(f"/contracts/{contract['booking_id']}", headers=v2["headers"])
    assert resp.status_code == 403, resp.text


def test_deposit_percent_must_be_between_0_and_100():
    v = _setup_vendor()
    resp = client.post(
        "/contracts",
        json={
            "service_id": v["service_id"], "date_iso": "2027-09-01", "time_start": "10:00",
            "time_end": "14:00", "amount_cents": 100_000, "deposit_percent": 150,
        },
        headers=v["headers"],
    )
    assert resp.status_code == 400, resp.text


def test_conflicting_date_and_time_is_refused():
    v = _setup_vendor()
    _create(v, date_iso="2027-10-10", time_start="18:00", time_end="22:00")
    resp = client.post(
        "/contracts",
        json={
            "service_id": v["service_id"], "date_iso": "2027-10-10",
            "time_start": "19:00", "time_end": "23:00", "amount_cents": 50_000,
        },
        headers=v["headers"],
    )
    assert resp.status_code == 409, resp.text
