"""Vendors and packages say which service they are (docs/DECISIONS.md #35):
specialties and service_key from pickers, the old category/subcategory kept
in step for older clients, the "pick your services" prompt, search by
service, and bundle slots matched by service."""

import uuid

import pytest

from app.db.models import Service, User, Vendor
from app.models.chatbot_schemas import ChatbotState
from app.services.chatbot_service import _candidate_service_rows
from tests.test_api import TestingSessionLocal, client, make_auth_headers


@pytest.fixture(autouse=True)
def _clean_up():
    """Leave nothing behind for the search tests that page through results."""
    yield
    db = TestingSessionLocal()
    users = [u for (u,) in db.query(User.user_id).filter(User.email.startswith("sk_", autoescape=True), User.email.endswith("@test.com"))]
    vendors = [v for (v,) in db.query(Vendor.vendor_id).filter(Vendor.user_id.in_(users))]
    db.query(Service).filter(Service.vendor_id.in_(vendors)).delete(synchronize_session=False)
    db.query(Vendor).filter(Vendor.vendor_id.in_(vendors)).delete(synchronize_session=False)
    db.query(User).filter(User.user_id.in_(users)).delete(synchronize_session=False)
    db.commit()
    db.close()


def _signed_in(prefix: str) -> dict:
    uid = uuid.uuid4().hex[:8]
    db = TestingSessionLocal()
    user = User(
        email=f"{prefix}_{uid}@test.com", username=f"{prefix}_{uid}", password="pw",
        phone="1234567890", f_name="Test", l_name="Vendor", age=30, location="10001",
        gender="Test", language="English", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    headers = make_auth_headers(user)
    db.close()
    return headers


def _vendor(prefix: str, **body) -> tuple[dict, dict]:
    headers = _signed_in(prefix)
    r = client.post("/vendors", json={"bio": "Test bio", **body}, headers=headers)
    assert r.status_code == 200, r.text
    return headers, r.json()


def _package(headers, **body):
    return client.post("/services", json={"name": "Package", "price": 500, **body}, headers=headers)


# ── Vendors ──────────────────────────────────────────────────────────

def test_specialties_set_the_old_fields_older_clients_read():
    _, v = _vendor("sk_new", specialties=["dj", "qawwali", "dj"])
    assert v["specialties"] == ["dj", "qawwali"]
    assert v["specializations"] == [
        {"category": "music_entertainment", "subcategory": "dj"},
        {"category": "music_entertainment", "subcategory": None},
    ]
    assert (v["category"], v["subcategory"]) == ("music_entertainment", "dj")
    assert v["service_picks_needed"] == []


def test_a_vague_old_category_asks_the_vendor_to_pick_until_they_do():
    headers, v = _vendor("sk_old", specializations=[{"category": "beauty"}, {"category": "photography"}])
    assert v["specialties"] == ["photography"]
    [pick] = v["service_picks_needed"]
    assert pick["category"] == "beauty" and "mehndi_artist" in pick["choices"]

    picked = client.patch("/vendors/me", json={"specialties": ["photography", "mehndi_artist"]}, headers=headers)
    assert picked.status_code == 200, picked.text
    assert picked.json()["service_picks_needed"] == []

    # An older client saving the old pairs keeps what was picked inside them.
    old = client.patch(
        "/vendors/me", json={"specializations": [{"category": "beauty"}, {"category": "photography"}]},
        headers=headers,
    ).json()
    assert old["specialties"] == ["mehndi_artist", "photography"]
    assert old["service_picks_needed"] == []


def test_unknown_or_too_many_specialties_are_refused():
    headers, _ = _vendor("sk_bad", specialties=["dj"])
    r = client.patch("/vendors/me", json={"specialties": ["dj", "juggler"]}, headers=headers)
    assert r.status_code == 400 and "juggler" in r.json()["detail"]
    many = ["dj", "dhol", "tabla", "sitar", "veena", "bansuri", "shehnai", "harmonium", "sarangi", "mc",
            "qawwali", "vocalist", "live_band", "brass_band", "nadaswaram", "fire_act", "kirtan", "mandap",
            "floral", "decor", "tent"]
    assert client.patch("/vendors/me", json={"specialties": many}, headers=headers).status_code == 422


# ── Packages ─────────────────────────────────────────────────────────

def test_a_package_names_its_service_and_details():
    headers, _ = _vendor("sk_pkg", specialties=["catering"])
    r = _package(headers, service_key="catering", attributes={"cuisine": ["punjabi", "gujarati"], "dietary": ["jain"]})
    assert r.status_code == 200, r.text
    p = r.json()
    assert (p["service_key"], p["category"], p["subcategory"]) == ("catering", "catering", None)
    assert p["attributes"] == {"cuisine": ["punjabi", "gujarati"], "dietary": ["jain"]}

    bad_attr = _package(headers, service_key="catering", attributes={"capacity": 200})
    assert bad_attr.status_code == 400 and "isn't a detail of Caterer" in bad_attr.json()["detail"]
    bad_option = _package(headers, service_key="catering", attributes={"cuisine": ["martian"]})
    assert bad_option.status_code == 400
    assert _package(headers, service_key="juggler").status_code == 400

    # Another service's details don't carry over; a new service joins the
    # vendor's specialties.
    moved = client.patch(f"/services/{p['service_id']}", json={"service_key": "food_truck"}, headers=headers).json()
    assert moved["service_key"] == "food_truck" and moved["attributes"] == {}
    assert client.get("/vendors/me", headers=headers).json()["specialties"] == ["catering", "food_truck"]


def test_an_old_client_package_finds_its_service_through_the_vendor():
    headers, _ = _vendor("sk_legacy", specialties=["decor"])
    vague = _package(headers, category="floral_decor").json()
    assert vague["service_key"] == "decor"
    exact = _package(headers, category="beauty", subcategory="mehndi_artist").json()
    assert exact["service_key"] == "mehndi_artist"

    headers2, _ = _vendor("sk_legacy2", specializations=[{"category": "beauty"}])
    unknown = _package(headers2, category="beauty").json()
    assert unknown["service_key"] is None  # the vendor is asked to pick


# ── Search and bundles ───────────────────────────────────────────────

def test_search_by_service_or_group():
    headers, v = _vendor("sk_search", specialties=["dj", "mehndi_artist"])
    dj = _package(headers, name=f"DJ {uuid.uuid4().hex[:6]}", service_key="dj").json()
    mehndi = _package(headers, name=f"Mehndi {uuid.uuid4().hex[:6]}", service_key="mehndi_artist").json()

    def found(service):
        rows = client.get("/vendors/search", params={"service": service, "limit": 100}).json()["items"]
        return {r.get("service_id") or r.get("service", {}).get("service_id") for r in rows}

    assert dj["service_id"] in found("dj") and mehndi["service_id"] not in found("dj")
    assert mehndi["service_id"] in found("beauty")
    assert {dj["service_id"], mehndi["service_id"]} <= found("dj,mehndi_artist")
    assert found("juggler") == set()


def test_a_bundle_slot_matches_by_service_and_the_old_category_as_a_fallback():
    db = TestingSessionLocal()
    try:
        vendor = Vendor(user_id=_user_id(db), bio="b", category="music_entertainment", rating=4.5, num_events=0)
        db.add(vendor)
        db.flush()
        dj = Service(vendor_id=vendor.vendor_id, name="Bundle DJ", price=900, category="music_entertainment",
                     subcategory="dj", service_key="dj", experience="x", negotiable=False)
        old = Service(vendor_id=vendor.vendor_id, name="Old troupe", price=900, category="cultural_services",
                      experience="x", negotiable=False)
        db.add_all([dj, old])
        db.commit()
        # Cultural Services became the Entertainment group: a DJ now fills it,
        # and an old cultural package with no service yet still does.
        def slot(cat):
            rows = _candidate_service_rows(cat, ChatbotState(), db, set(), float("inf"), set())
            return {s.service_id for s, _, _ in rows}

        assert {dj.service_id, old.service_id} <= slot("cultural_services")
        assert dj.service_id in slot("dj") and old.service_id not in slot("dj")
    finally:
        db.close()


def _user_id(db) -> str:
    uid = uuid.uuid4().hex[:8]
    u = User(email=f"sk_b_{uid}@test.com", username=f"sk_b_{uid}", password="pw", phone="1", f_name="B",
             l_name="V", age=30, location="NJ", gender="x", language="en", token_version=0)
    db.add(u)
    db.flush()
    return u.user_id
