"""End-to-end coverage for the vendor-authored "Contracts" flow: a vendor
creates a guest booking, the public link lets a client (no login, ever)
read it, fill in their own details, and e-sign it, and both sides can
self-report the deposit/full payment afterward. See docs/DECISIONS.md #13.
"""

import uuid

import pytest

from app.db.models import Booking, Service, User, Vendor
from app.limiter import limiter
from tests.test_api import TestingSessionLocal, client, make_auth_headers

_created = {"users": [], "vendors": [], "services": [], "bookings": []}


@pytest.fixture(autouse=True)
def _isolate():
    """Rate limits are shared across the whole suite's one test-client
    address, and this file alone makes more /sign calls than the 5/minute
    limit allows (same reasoning as test_bookings.py's without_rate_limits).
    Also cleans up every row this file creates so later test files' search/
    listing assertions (which assume a known, small dataset) don't see them.
    """
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
        email=f"contract_v_{uid}@test.com", username=f"contract_v_{uid}", password="pw",
        phone="1234567890", f_name="Arjun", l_name="Kapoor", age=30, location="NJ",
        gender="Test", language="EN", token_version=0,
    )
    db.add(vendor_user); db.commit(); db.refresh(vendor_user)
    vendor = Vendor(
        user_id=vendor_user.user_id, bio="DJ", category="music", rating=0.0, num_events=0,
        payment_method="manual", venmo_handle="@arjun-dj", zelle_contact="arjun@example.com",
    )
    db.add(vendor); db.commit(); db.refresh(vendor)
    service = Service(
        name="4-Hour Reception Package", price=1400.0, price_unit="event",
        vendor_id=vendor.vendor_id, experience="e", category="music", negotiable=False,
    )
    db.add(service); db.commit(); db.refresh(service)
    headers = make_auth_headers(vendor_user)
    result = {"vendor_user_id": vendor_user.user_id, "vendor_id": vendor.vendor_id,
              "service_id": service.service_id, "headers": headers}
    db.close()
    _created["users"].append(result["vendor_user_id"])
    _created["vendors"].append(result["vendor_id"])
    _created["services"].append(result["service_id"])
    return result


def _create_contract(v, **overrides):
    body = {
        "service_id": v["service_id"],
        "date_iso": "2027-11-13",
        "time_start": "19:00",
        "time_end": "23:00",
        "amount_cents": 140_000,
        "deposit_percent": 50,
        "cancellation_window_hours": 720,
        "overtime_rate_cents": 15_000,
        "contract_terms": {"equipment_power": "Vendor brings all gear."},
    }
    body.update(overrides)
    resp = client.post("/contracts", json=body, headers=v["headers"])
    assert resp.status_code == 201, resp.text
    _created["bookings"].append(resp.json()["booking_id"])
    return resp.json()


def test_vendor_creates_a_guest_contract_with_no_client_account():
    v = _setup_vendor()
    contract = _create_contract(v)
    assert contract["deposit_amount_cents"] == 70_000
    assert contract["location"] == "TBD"
    assert contract["signed_at"] is None
    assert contract["contract_token"]

    db = TestingSessionLocal()
    booking = db.query(Booking).filter(Booking.booking_id == contract["booking_id"]).first()
    assert booking.user_id is None
    assert booking.status == "approved"
    db.close()


def test_vendor_cannot_create_a_contract_for_someone_elses_service():
    v = _setup_vendor()
    other = _setup_vendor()
    resp = client.post(
        "/contracts",
        json={
            "service_id": other["service_id"], "date_iso": "2027-11-13",
            "time_start": "19:00", "time_end": "23:00", "amount_cents": 140_000,
        },
        headers=v["headers"],
    )
    assert resp.status_code == 404, resp.text


def test_public_link_full_flow_read_fill_sign():
    v = _setup_vendor()
    contract = _create_contract(v)
    token = contract["contract_token"]

    read = client.get(f"/guest-bookings/{token}")
    assert read.status_code == 200, read.text
    assert read.json()["vendor_venmo_handle"] == "@arjun-dj"
    assert read.json()["signed_at"] is None

    filled = client.patch(
        f"/guest-bookings/{token}",
        json={
            "guest_name": "Priya Mehta", "guest_email": "priya@example.com",
            "guest_phone": "7325550101", "location": "Pines Manor, Edison, NJ",
            "guest_count": 180,
        },
    )
    assert filled.status_code == 200, filled.text
    assert filled.json()["location"] == "Pines Manor, Edison, NJ"

    signed = client.post(f"/guest-bookings/{token}/sign", json={"signer_name": "Priya Mehta"})
    assert signed.status_code == 200, signed.text
    assert signed.json()["signer_name"] == "Priya Mehta"
    assert signed.json()["signed_at"] is not None


def test_signing_requires_an_email_on_file():
    v = _setup_vendor()
    contract = _create_contract(v)
    token = contract["contract_token"]

    resp = client.post(f"/guest-bookings/{token}/sign", json={"signer_name": "Priya Mehta"})
    assert resp.status_code == 400, resp.text
    assert "email" in resp.json()["detail"].lower()


def test_cannot_edit_details_or_sign_twice_after_signing():
    v = _setup_vendor()
    contract = _create_contract(v)
    token = contract["contract_token"]
    client.patch(f"/guest-bookings/{token}", json={"guest_email": "priya@example.com"})
    client.post(f"/guest-bookings/{token}/sign", json={"signer_name": "Priya Mehta"})

    again = client.post(f"/guest-bookings/{token}/sign", json={"signer_name": "Priya Mehta"})
    assert again.status_code == 400, again.text

    edit = client.patch(f"/guest-bookings/{token}", json={"guest_name": "Someone Else"})
    assert edit.status_code == 400, edit.text


def test_bad_token_returns_404_not_500():
    resp = client.get("/guest-bookings/not-a-real-token")
    assert resp.status_code == 404, resp.text


def test_guest_booking_shows_contract_fields_in_the_vendors_general_list():
    """The vendor's ordinary bookings list (GET /bookings/vendor, what the
    dashboard/pipeline/my-bookings all fetch) must expose the guest/contract
    fields too -- a pipeline view can't derive a stage without them."""
    v = _setup_vendor()
    contract = _create_contract(v)

    listed = client.get(f"/bookings/vendor/{v['vendor_id']}", headers=v["headers"])
    assert listed.status_code == 200, listed.text
    row = next(b for b in listed.json()["items"] if b["booking_id"] == contract["booking_id"])
    assert row["is_guest_booking"] is True
    assert row["deposit_percent"] == 50
    assert row["signed_at"] is None
    assert row["contract_token"] == contract["contract_token"]


def test_vendor_cannot_read_own_authenticated_booking_via_guest_endpoint():
    """A real-account booking (user_id set) must never be reachable through
    the token-based public router, even if someone guessed/leaked its id."""
    uid = uuid.uuid4().hex[:8]
    db = TestingSessionLocal()
    vendor_user = User(
        email=f"real_v_{uid}@test.com", username=f"real_v_{uid}", password="pw",
        phone="1", f_name="V", l_name="N", age=30, location="NJ",
        gender="F", language="EN", token_version=0,
    )
    client_user = User(
        email=f"real_c_{uid}@test.com", username=f"real_c_{uid}", password="pw",
        phone="1", f_name="C", l_name="L", age=30, location="NJ",
        gender="F", language="EN", token_version=0,
    )
    db.add_all([vendor_user, client_user]); db.commit()
    db.refresh(vendor_user); db.refresh(client_user)
    vendor = Vendor(
        user_id=vendor_user.user_id, bio="b", category="venue", rating=0.0, num_events=0,
        payment_method="manual",
    )
    db.add(vendor); db.commit(); db.refresh(vendor)
    service = Service(
        name="svc", price=100.0, price_unit="event", vendor_id=vendor.vendor_id,
        experience="e", category="venue", negotiable=False,
    )
    db.add(service); db.commit(); db.refresh(service)
    booking = Booking(
        user_id=client_user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        date_iso="2027-01-01", time_start="10:00", time_end="12:00", location="loc",
        status="pending", payment_status="unpaid",
    )
    db.add(booking); db.commit(); db.refresh(booking)
    booking_id = booking.booking_id
    _created["bookings"].append(booking_id)
    _created["services"].append(service.service_id)
    _created["vendors"].append(vendor.vendor_id)
    _created["users"].extend([vendor_user.user_id, client_user.user_id])
    db.close()

    resp = client.get(f"/guest-bookings/{booking_id}")
    assert resp.status_code == 404, resp.text
