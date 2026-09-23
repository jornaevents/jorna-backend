"""Package (Service) details from 0063: status (active/hidden/archived),
inclusions, add-ons, per-package terms, ordering, and years of experience
moving to the vendor — plus where each status is and isn't visible.
"""

import uuid

from app.db.models import Booking, Service, User
from tests.test_api import TestingSessionLocal, client, make_auth_headers
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _created,
    _isolate,
    _setup_vendor,
)


def _package(v, **fields):
    body = {"name": "Sangeet DJ set", "price": 1200, "price_unit": "event", **fields}
    resp = client.post("/services", json=body, headers=v["headers"])
    assert resp.status_code == 200, resp.text
    _created["services"].append(resp.json()["service_id"])
    return resp.json()


def _client_user():
    uid = uuid.uuid4().hex[:8]
    db = TestingSessionLocal()
    user = User(
        email=f"pkg_c_{uid}@test.com", username=f"pkg_c_{uid}", password="pw",
        phone="1234567890", f_name="Client", l_name="Test", age=30, location="NJ",
        gender="Test", language="EN", token_version=0,
    )
    db.add(user); db.commit(); db.refresh(user)
    headers = make_auth_headers(user)
    _created["users"].append(user.user_id)
    db.close()
    return headers


def _own_list(v, **params):
    return client.get(
        "/services", params={"vendor_id": v["vendor_id"], "limit": 100, **params},
        headers=v["headers"],
    ).json()["items"]


def test_creates_a_package_with_details_and_add_ons():
    v = _setup_vendor()
    client.patch("/vendors/me", json={"years_experience": 9}, headers=v["headers"])
    pkg = _package(
        v,
        included_hours=4,
        inclusions=["Sound system", "  ", "Two wireless mics"],
        add_ons=[
            {"name": "Extra hour", "price": 250, "price_unit": "hour"},
            {"name": "Dhol player", "price": 400},
        ],
        deposit_percent=30,
        cancellation_window_hours=720,
    )
    assert pkg["status"] == "active"
    assert pkg["included_hours"] == 4
    assert pkg["inclusions"] == ["Sound system", "Two wireless mics"]  # blanks dropped
    assert [a["name"] for a in pkg["add_ons"]] == ["Extra hour", "Dhol player"]
    assert all(a["id"] for a in pkg["add_ons"])                         # ids assigned
    assert pkg["add_ons"][1]["price_unit"] == "event"
    assert pkg["deposit_percent"] == 30
    # experience left out → filled from the vendor, for older clients.
    assert pkg["experience"] == "9 years"


def test_rejects_bad_add_ons_and_terms():
    v = _setup_vendor()
    for bad in (
        {"add_ons": [{"name": "Extra", "price": 100, "price_unit": "day"}]},
        {"add_ons": [{"name": "", "price": 100}]},
        {"add_ons": [{"name": "Free", "price": 0}]},
        {"deposit_percent": 101},
        {"status": "deleted"},
    ):
        resp = client.post(
            "/services", json={"name": "X", "price": 10, **bad}, headers=v["headers"],
        )
        assert resp.status_code == 422, (bad, resp.text)


def test_hidden_packages_are_the_owners_business_only():
    v = _setup_vendor()  # comes with one active package
    hidden = _package(v, name="Private corporate set", status="hidden")

    public = client.get("/services", params={"vendor_id": v["vendor_id"]}).json()["items"]
    assert hidden["service_id"] not in [s["service_id"] for s in public]

    # A stranger asking for unlisted packages still only gets active ones.
    stranger = _setup_vendor()
    peek = client.get(
        "/services",
        params={"vendor_id": v["vendor_id"], "include_unlisted": True},
        headers=stranger["headers"],
    ).json()["items"]
    assert hidden["service_id"] not in [s["service_id"] for s in peek]

    mine = _own_list(v, include_unlisted=True)
    assert hidden["service_id"] in [s["service_id"] for s in mine]

    search = client.get("/vendors/search", params={"service_name": "Private corporate"}).json()
    assert search["items"] == []


def test_a_hidden_package_can_be_contracted_but_not_booked():
    v = _setup_vendor()
    hidden = _package(v, status="hidden")

    booking = client.post(
        "/bookings",
        json={
            "service_id": hidden["service_id"], "event_name": "Test", "time_start": "18:00",
            "time_end": "22:00", "location": "Hall", "date_iso": "2027-12-10",
        },
        headers=_client_user(),
    )
    assert booking.status_code == 409, booking.text

    contract = client.post(
        "/contracts",
        json={
            "service_id": hidden["service_id"], "date_iso": "2027-12-11",
            "time_start": "18:00", "time_end": "22:00", "amount_cents": 120_000,
        },
        headers=v["headers"],
    )
    assert contract.status_code == 201, contract.text
    _created["bookings"].append(contract.json()["booking_id"])


def test_archived_packages_cant_start_a_contract():
    v = _setup_vendor()
    archived = _package(v, status="archived")
    resp = client.post(
        "/contracts",
        json={
            "service_id": archived["service_id"], "date_iso": "2027-12-12",
            "time_start": "18:00", "time_end": "22:00", "amount_cents": 1,
        },
        headers=v["headers"],
    )
    assert resp.status_code == 400
    assert "archived" in resp.json()["detail"]


def test_deleting_a_package_with_bookings_archives_it_instead():
    v = _setup_vendor()
    unused = _package(v, name="Unused")
    used = _package(v, name="Used")
    contract = client.post(
        "/contracts",
        json={
            "service_id": used["service_id"], "date_iso": "2027-12-13",
            "time_start": "18:00", "time_end": "22:00", "amount_cents": 50_000,
        },
        headers=v["headers"],
    )
    _created["bookings"].append(contract.json()["booking_id"])

    assert client.delete(f"/services/{unused['service_id']}", headers=v["headers"]).status_code == 204
    assert client.delete(f"/services/{used['service_id']}", headers=v["headers"]).status_code == 204

    db = TestingSessionLocal()
    assert db.query(Service).filter(Service.service_id == unused["service_id"]).first() is None
    kept = db.query(Service).filter(Service.service_id == used["service_id"]).first()
    assert kept is not None and kept.status == "archived"
    # The booking still reaches its package.
    assert db.query(Booking).filter(Booking.service_id == used["service_id"]).count() == 1
    db.close()

    # Archived packages stay out of the public list.
    public = client.get("/services", params={"vendor_id": v["vendor_id"]}).json()["items"]
    assert used["service_id"] not in [s["service_id"] for s in public]


def test_packages_list_in_the_vendors_order():
    v = _setup_vendor()  # its seeded package has no sort_order → sorts last
    b = _package(v, name="B", sort_order=2)
    a = _package(v, name="A", sort_order=1)
    names = [s["name"] for s in _own_list(v)]
    assert names[:2] == ["A", "B"]
    client.patch(f"/services/{a['service_id']}", json={"sort_order": 3}, headers=v["headers"])
    names = [s["name"] for s in _own_list(v)]
    assert names[:2] == [b["name"], "A"]


def test_explicit_null_status_on_update_is_ignored():
    v = _setup_vendor()
    pkg = _package(v)
    resp = client.patch(
        f"/services/{pkg['service_id']}", json={"status": None, "name": "Renamed"},
        headers=v["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"
    assert resp.json()["name"] == "Renamed"


def test_vendor_years_experience_round_trips():
    v = _setup_vendor()
    resp = client.patch("/vendors/me", json={"years_experience": 12}, headers=v["headers"])
    assert resp.status_code == 200, resp.text
    assert client.get("/vendors/me", headers=v["headers"]).json()["years_experience"] == 12
    assert client.patch(
        "/vendors/me", json={"years_experience": 120}, headers=v["headers"],
    ).status_code == 422

