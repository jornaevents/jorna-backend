"""The deposit-specific self-attestation pair (deposit_marked_paid_at/
deposit_confirmed_received_at), alongside the existing full-payment pair
(manual_payment_marked_at/confirmed_at, see test_manual_payment.py). Covers
both the guest/token-based side (guest_bookings router) and the
authenticated side (payments router) — see docs/DECISIONS.md #13.
"""

import uuid

import pytest

from app.db.models import Booking, Service, User, Vendor
from app.limiter import limiter
from tests.test_api import TestingSessionLocal, client, make_auth_headers

_created = {"users": [], "vendors": [], "services": [], "bookings": []}


@pytest.fixture(autouse=True)
def _isolate():
    """See test_guest_booking_flow.py's identical fixture for why: shared
    rate limits across the suite's one test-client address, and cleanup so
    other files' search/listing assertions don't see these rows."""
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


def _setup_guest_contract(*, deposit_percent=50):
    uid = uuid.uuid4().hex[:8]
    db = TestingSessionLocal()
    vendor_user = User(
        email=f"dep_v_{uid}@test.com", username=f"dep_v_{uid}", password="pw",
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
    result = {"vendor_user_id": vendor_user.user_id, "service_id": service.service_id, "headers": headers}
    _created["users"].append(vendor_user.user_id)
    _created["vendors"].append(vendor.vendor_id)
    _created["services"].append(service.service_id)
    db.close()

    contract_body = {
        "service_id": result["service_id"], "date_iso": "2027-08-01",
        "time_start": "10:00", "time_end": "14:00", "amount_cents": 100_000,
    }
    if deposit_percent is not None:
        contract_body["deposit_percent"] = deposit_percent
    resp = client.post("/contracts", json=contract_body, headers=result["headers"])
    assert resp.status_code == 201, resp.text
    contract = resp.json()
    _created["bookings"].append(contract["booking_id"])

    token = contract["contract_token"]
    client.patch(f"/guest-bookings/{token}", json={"guest_email": "guest@example.com"})
    signed = client.post(f"/guest-bookings/{token}/sign", json={"signer_name": "Guest Name"})
    assert signed.status_code == 200, signed.text

    result["token"] = token
    result["booking_id"] = contract["booking_id"]
    return result


def test_guest_marks_deposit_paid_then_vendor_confirms():
    ctx = _setup_guest_contract()
    marked = client.post(f"/guest-bookings/{ctx['token']}/mark-deposit-paid")
    assert marked.status_code == 200, marked.text

    confirmed = client.post(
        f"/payments/bookings/{ctx['booking_id']}/confirm-deposit-received", headers=ctx["headers"]
    )
    assert confirmed.status_code == 200, confirmed.text


def test_vendor_cannot_confirm_deposit_before_its_marked():
    ctx = _setup_guest_contract()
    resp = client.post(
        f"/payments/bookings/{ctx['booking_id']}/confirm-deposit-received", headers=ctx["headers"]
    )
    assert resp.status_code == 400, resp.text


def test_cannot_mark_deposit_paid_on_a_booking_with_no_deposit_configured():
    ctx = _setup_guest_contract(deposit_percent=None)
    resp = client.post(f"/guest-bookings/{ctx['token']}/mark-deposit-paid")
    assert resp.status_code == 400, resp.text


def test_guest_cannot_mark_deposit_paid_before_signing():
    uid = uuid.uuid4().hex[:8]
    db = TestingSessionLocal()
    vendor_user = User(
        email=f"unsigned_v_{uid}@test.com", username=f"unsigned_v_{uid}", password="pw",
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
    service_id = service.service_id
    _created["users"].append(vendor_user.user_id)
    _created["vendors"].append(vendor.vendor_id)
    _created["services"].append(service_id)
    db.close()

    resp = client.post(
        "/contracts",
        json={
            "service_id": service_id, "date_iso": "2027-08-01", "time_start": "10:00",
            "time_end": "14:00", "amount_cents": 100_000, "deposit_percent": 30,
        },
        headers=headers,
    )
    contract = resp.json()
    _created["bookings"].append(contract["booking_id"])
    token = contract["contract_token"]

    marked = client.post(f"/guest-bookings/{token}/mark-deposit-paid")
    assert marked.status_code == 400, marked.text


def test_authenticated_deposit_pair_for_a_real_account_booking():
    """A booking with a real client account can also have a deposit
    configured (not just guest bookings) -- the authenticated mark/confirm
    pair in payments.py covers that case."""
    uid = uuid.uuid4().hex[:8]
    db = TestingSessionLocal()
    client_user = User(
        email=f"dep_c_{uid}@test.com", username=f"dep_c_{uid}", password="pw",
        phone="1", f_name="C", l_name="L", age=30, location="NJ",
        gender="F", language="EN", token_version=0,
    )
    vendor_user = User(
        email=f"dep_v2_{uid}@test.com", username=f"dep_v2_{uid}", password="pw",
        phone="1", f_name="V", l_name="N", age=30, location="NJ",
        gender="F", language="EN", token_version=0,
    )
    db.add_all([client_user, vendor_user]); db.commit()
    db.refresh(client_user); db.refresh(vendor_user)
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
    booking = Booking(
        user_id=client_user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        date_iso="2027-08-01", time_start="10:00", time_end="14:00", location="loc",
        status="approved", payment_status="unpaid", amount_cents=100_000,
        payment_method="manual", deposit_percent=40, deposit_amount_cents=40_000,
    )
    db.add(booking); db.commit(); db.refresh(booking)
    booking_id = booking.booking_id
    client_headers = make_auth_headers(client_user)
    vendor_headers = make_auth_headers(vendor_user)
    _created["users"].extend([client_user.user_id, vendor_user.user_id])
    _created["vendors"].append(vendor.vendor_id)
    _created["services"].append(service.service_id)
    _created["bookings"].append(booking_id)
    db.close()

    marked = client.post(f"/payments/bookings/{booking_id}/mark-deposit-paid", headers=client_headers)
    assert marked.status_code == 200, marked.text

    confirmed = client.post(
        f"/payments/bookings/{booking_id}/confirm-deposit-received", headers=vendor_headers
    )
    assert confirmed.status_code == 200, confirmed.text
