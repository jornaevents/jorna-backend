"""GET /vendors/me/clients: a vendor's bookings grouped by client, for both
real accounts (grouped by user_id) and guest bookings (grouped by
guest_name+guest_phone, since there's no account to key on). See
app/services/contract_service.py's get_vendor_clients.
"""

import uuid

import pytest

from app.db.models import Booking, Service, User, Vendor
from tests.test_api import TestingSessionLocal, client, make_auth_headers

_created = {"users": [], "vendors": [], "services": [], "bookings": []}


@pytest.fixture(autouse=True)
def _isolate():
    """See test_guest_booking_flow.py's identical fixture."""
    yield
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
        email=f"crm_v_{uid}@test.com", username=f"crm_v_{uid}", password="pw",
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
    result = {"vendor_id": vendor.vendor_id, "service_id": service.service_id, "headers": headers}
    _created["users"].append(vendor_user.user_id)
    _created["vendors"].append(vendor.vendor_id)
    _created["services"].append(service.service_id)
    db.close()
    return result


def test_empty_clients_list_for_a_new_vendor():
    v = _setup_vendor()
    resp = client.get("/vendors/me/clients", headers=v["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": [], "total": 0}


def test_guest_bookings_group_by_name_and_phone_with_lifetime_value():
    v = _setup_vendor()
    db = TestingSessionLocal()
    for amount in (100_000, 200_000):
        booking = Booking(
            user_id=None, vendor_id=v["vendor_id"], service_id=v["service_id"],
            date_iso="2027-05-01", time_start="10:00", time_end="12:00", location="TBD",
            status="approved", payment_status="unpaid", amount_cents=amount,
            payment_method="manual", guest_name="Priya Mehta", guest_phone="7325551234",
            guest_email="priya@example.com",
        )
        db.add(booking); db.commit(); db.refresh(booking)
        _created["bookings"].append(booking.booking_id)
    db.close()

    resp = client.get("/vendors/me/clients", headers=v["headers"])
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "Priya Mehta"
    assert items[0]["is_guest"] is True
    assert items[0]["event_count"] == 2
    assert items[0]["lifetime_value_cents"] == 300_000
    assert items[0]["repeat_client"] is True


def test_a_rejected_booking_does_not_count_toward_lifetime_value():
    v = _setup_vendor()
    db = TestingSessionLocal()
    booking = Booking(
        user_id=None, vendor_id=v["vendor_id"], service_id=v["service_id"],
        date_iso="2027-05-01", time_start="10:00", time_end="12:00", location="TBD",
        status="rejected", payment_status="unpaid", amount_cents=100_000,
        payment_method="manual", guest_name="Declined Client", guest_phone="7325559999",
    )
    db.add(booking); db.commit(); db.refresh(booking)
    _created["bookings"].append(booking.booking_id)
    db.close()

    resp = client.get("/vendors/me/clients", headers=v["headers"])
    assert resp.json() == {"items": [], "total": 0}


def test_real_account_bookings_group_by_user_id_not_name():
    v = _setup_vendor()
    db = TestingSessionLocal()
    uid = uuid.uuid4().hex[:8]
    real_client = User(
        email=f"crm_c_{uid}@test.com", username=f"crm_c_{uid}", password="pw",
        phone="5551112222", f_name="Rahul", l_name="Sharma", age=30, location="NJ",
        gender="M", language="EN", token_version=0,
    )
    db.add(real_client); db.commit(); db.refresh(real_client)
    booking = Booking(
        user_id=real_client.user_id, vendor_id=v["vendor_id"], service_id=v["service_id"],
        date_iso="2027-05-01", time_start="10:00", time_end="12:00", location="loc",
        status="approved", payment_status="unpaid", amount_cents=320_000,
        payment_method="manual",
    )
    db.add(booking); db.commit(); db.refresh(booking)
    _created["users"].append(real_client.user_id)
    _created["bookings"].append(booking.booking_id)
    db.close()

    resp = client.get("/vendors/me/clients", headers=v["headers"])
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "Rahul Sharma"
    assert items[0]["is_guest"] is False
    assert items[0]["repeat_client"] is False
