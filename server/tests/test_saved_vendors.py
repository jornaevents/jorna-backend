"""Saved vendors (0088): a client hearts vendors in Explore and compares them."""

from tests.test_api import client
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _isolate,
    _setup_vendor,
)
from tests.test_package_details import _client_user


def test_a_client_saves_lists_and_removes_vendors():
    a, b = _setup_vendor(), _setup_vendor()
    me = _client_user()

    assert client.get("/saved-vendors", headers=me).json() == {"items": []}
    assert client.put(f"/saved-vendors/{a['vendor_id']}", headers=me).status_code == 204
    # Saving twice is saving once.
    assert client.put(f"/saved-vendors/{a['vendor_id']}", headers=me).status_code == 204
    assert client.put(f"/saved-vendors/{b['vendor_id']}", headers=me).status_code == 204

    items = client.get("/saved-vendors", headers=me).json()["items"]
    assert sorted(i["vendor_id"] for i in items) == sorted([a["vendor_id"], b["vendor_id"]])
    first = next(i for i in items if i["vendor_id"] == a["vendor_id"])
    assert first["name"] and "rating" in first and "years_experience" in first
    # The vendor's listed package sets the starting price.
    assert first["starting_price"] is not None

    assert client.delete(f"/saved-vendors/{a['vendor_id']}", headers=me).status_code == 204
    # Removing one that isn't saved is fine.
    assert client.delete(f"/saved-vendors/{a['vendor_id']}", headers=me).status_code == 204
    assert [i["vendor_id"] for i in client.get("/saved-vendors", headers=me).json()["items"]] == [b["vendor_id"]]


def test_saved_vendors_are_each_clients_own_and_need_a_real_vendor():
    v = _setup_vendor()
    me, other = _client_user(), _client_user()
    client.put(f"/saved-vendors/{v['vendor_id']}", headers=me)
    assert client.get("/saved-vendors", headers=other).json() == {"items": []}
    assert client.put("/saved-vendors/not-a-vendor", headers=me).status_code == 404
    assert client.get("/saved-vendors").status_code in (401, 403)
