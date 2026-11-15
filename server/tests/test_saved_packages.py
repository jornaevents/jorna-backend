"""Saved packages (0090): a client hearts packages in Explore and compares them."""

from tests.test_api import client
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _isolate,
    _setup_vendor,
)
from tests.test_package_details import _client_user, _package


def test_a_client_saves_lists_and_removes_packages():
    v = _setup_vendor()
    dinner = _package(v, name="Plated dinner", price=38, price_unit="person",
                      inclusions=["Servers", "Linens"], add_ons=[{"name": "Chai bar", "price": 400}])
    buffet = _package(v, name="Buffet", price=8000, price_unit="event")
    me = _client_user()

    assert client.get("/saved-packages", headers=me).json() == {"items": []}
    assert client.put(f"/saved-packages/{dinner['service_id']}", headers=me).status_code == 204
    # Saving twice is saving once.
    assert client.put(f"/saved-packages/{dinner['service_id']}", headers=me).status_code == 204
    # Two packages from the same vendor are two separate entries — the point.
    assert client.put(f"/saved-packages/{buffet['service_id']}", headers=me).status_code == 204

    items = client.get("/saved-packages", headers=me).json()["items"]
    assert sorted(i["name"] for i in items) == ["Buffet", "Plated dinner"]
    d = next(i for i in items if i["name"] == "Plated dinner")
    assert d["price"] == 38 and d["price_unit"] == "person"
    assert d["inclusions"] == ["Servers", "Linens"] and d["add_ons"] == ["Chai bar"]
    assert d["vendor_id"] == v["vendor_id"] and d["vendor_name"] and d["available"] is True

    assert client.delete(f"/saved-packages/{dinner['service_id']}", headers=me).status_code == 204
    # Removing one that isn't saved is fine.
    assert client.delete(f"/saved-packages/{dinner['service_id']}", headers=me).status_code == 204
    assert [i["name"] for i in client.get("/saved-packages", headers=me).json()["items"]] == ["Buffet"]


def test_saved_packages_are_each_clients_own_and_need_a_live_package():
    v = _setup_vendor()
    pkg = _package(v, name="Sangeet set")
    me, other = _client_user(), _client_user()
    client.put(f"/saved-packages/{pkg['service_id']}", headers=me)
    assert client.get("/saved-packages", headers=other).json() == {"items": []}
    assert client.put("/saved-packages/not-a-package", headers=me).status_code == 404
    assert client.get("/saved-packages").status_code in (401, 403)

    # Hidden after saving: stays on the shortlist, marked unavailable, and can't
    # be saved fresh by anyone else.
    r = client.patch(f"/services/{pkg['service_id']}", json={"status": "hidden"}, headers=v["headers"])
    assert r.status_code == 200, r.text
    [item] = client.get("/saved-packages", headers=me).json()["items"]
    assert item["available"] is False
    assert client.put(f"/saved-packages/{pkg['service_id']}", headers=other).status_code == 404
