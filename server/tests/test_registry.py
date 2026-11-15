"""An event's gift registry (0089): funds, gifts recorded against them, and
thank-yous. Tracking only, and the event owner's alone."""

from tests.test_api import client
from tests.test_guest_booking_flow import _isolate  # noqa: F401 — autouse fixture
from tests.test_package_details import _client_user


def _event(headers):
    r = client.post("/events", json={"name": "Wedding", "date_iso": "2027-05-22", "location": "Edison, NJ"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["event_id"]


def test_funds_and_gifts_add_up():
    me = _client_user()
    event_id = _event(me)
    assert client.get(f"/events/{event_id}/registry", headers=me).json()["funds"] == []

    reg = client.post(f"/events/{event_id}/registry/funds", json={"name": " Honeymoon fund ", "goal_cents": 500_000}, headers=me)
    assert reg.status_code == 201, reg.text
    fund = reg.json()["funds"][0]
    assert fund["name"] == "Honeymoon fund" and fund["raised_cents"] == 0

    client.post(f"/registry/funds/{fund['fund_id']}/gifts", json={"from_name": "Rekha Bua", "amount_cents": 25_000}, headers=me)
    reg = client.post(f"/registry/funds/{fund['fund_id']}/gifts", json={"from_name": "The Patels", "amount_cents": 50_000, "thanked": True}, headers=me).json()
    assert reg["total_cents"] == 75_000 and reg["gift_count"] == 2 and reg["thanked_count"] == 1
    assert reg["funds"][0]["raised_cents"] == 75_000

    # Thanking, editing and removing a gift; clearing a goal; deleting a fund.
    gift = next(g for g in reg["funds"][0]["gifts"] if g["from_name"] == "Rekha Bua")
    reg = client.patch(f"/registry/gifts/{gift['gift_id']}", json={"thanked": True, "amount_cents": 30_000}, headers=me).json()
    assert reg["thanked_count"] == 2 and reg["total_cents"] == 80_000
    reg = client.delete(f"/registry/gifts/{gift['gift_id']}", headers=me).json()
    assert reg["gift_count"] == 1
    reg = client.patch(f"/registry/funds/{fund['fund_id']}", json={"goal_cents": None, "name": "Travel"}, headers=me).json()
    assert reg["funds"][0]["goal_cents"] is None and reg["funds"][0]["name"] == "Travel"
    reg = client.delete(f"/registry/funds/{fund['fund_id']}", headers=me).json()
    assert reg["funds"] == [] and reg["total_cents"] == 0


def test_a_registry_is_the_event_owners_alone_and_checks_what_it_is_given():
    me, other = _client_user(), _client_user()
    event_id = _event(me)
    fund_id = client.post(f"/events/{event_id}/registry/funds", json={"name": "Home"}, headers=me).json()["funds"][0]["fund_id"]

    assert client.get(f"/events/{event_id}/registry", headers=other).status_code == 404
    assert client.post(f"/events/{event_id}/registry/funds", json={"name": "Mine now"}, headers=other).status_code == 404
    assert client.post(f"/registry/funds/{fund_id}/gifts", json={"from_name": "X", "amount_cents": 100}, headers=other).status_code == 404
    assert client.delete(f"/registry/funds/{fund_id}", headers=other).status_code == 404

    assert client.post(f"/events/{event_id}/registry/funds", json={"name": ""}, headers=me).status_code == 422
    assert client.post(f"/registry/funds/{fund_id}/gifts", json={"from_name": "X", "amount_cents": 0}, headers=me).status_code == 422
