"""The service taxonomy (docs/DECISIONS.md #35): its shape, GET /taxonomy,
and that everything stored or sent today maps onto it."""

from app import taxonomy
from app.models.chatbot_schemas import CHATBOT_SLOTS
from app.models.schemas import VENDOR_SUBCATEGORIES, VendorCategory
from app.taxonomy.data import GROUPS, SERVICES
from tests.test_api import client


def test_the_shape_the_user_agreed():
    assert len(GROUPS) == 17 and len(SERVICES) == 73
    # Decided 2026-10-20: DJ is Entertainment, not Music.
    assert taxonomy.group_of("dj") == "entertainment"
    assert taxonomy.expand("music")[:2] == ["dhol", "live_band"]
    # Today's 10 slots with Floral & Decor split, plus Bar & bartending; the
    # old Cultural Services slot is the Entertainment group.
    assert len(taxonomy.bundle_services()) == 11 and "bar_beverage" in taxonomy.bundle_services()
    assert taxonomy.bundle_groups() == ["entertainment"]
    ranks = sorted(s["popular"] for s in SERVICES if s["popular"])
    assert ranks == list(range(1, 13))


def test_every_category_and_subcategory_stored_today_maps():
    for category in VendorCategory:
        assert category.value in taxonomy.data.LEGACY_CATEGORIES, category
        assert taxonomy.legacy_search(category.value), category
        for sub in VENDOR_SUBCATEGORIES.get(category.value, []):
            assert taxonomy.from_legacy(category.value, sub), (category, sub)


def test_a_bare_category_maps_or_asks_the_vendor_to_pick():
    assert taxonomy.from_legacy("photography") == "photography"
    assert taxonomy.from_legacy("attire", "groomsmen") == "attire"
    assert taxonomy.from_legacy("floral_decor", "decor") == "decor"
    # Ambiguous on its own — the vendor picks (decided 2026-10-20 for planning).
    ambiguous = {c for c, rule in taxonomy.data.LEGACY_CATEGORIES.items() if rule["service"] is None}
    assert ambiguous == {
        "planning", "cakes_desserts", "music_entertainment", "floral_decor", "beauty", "officiants",
        "cultural_services", "post_wedding",
    }
    assert all(taxonomy.from_legacy(c) is None for c in ambiguous)
    assert taxonomy.from_legacy(None) is None and taxonomy.from_legacy("nope") is None


def test_old_search_keys_still_find_the_same_vendors():
    music = taxonomy.legacy_search("music_entertainment")
    assert {"dj", "dhol", "mc", "tabla"} <= set(music) and "bhangra_troupe" not in music
    assert set(taxonomy.legacy_search("floral_decor")) == {"floral", "decor", "mandap"}
    # Search already takes a subcategory as a category ("dj"); still does.
    assert taxonomy.legacy_search("dj") == ["dj"]
    assert taxonomy.legacy_search("mehndi_artist") == ["mehndi_artist"]
    assert taxonomy.legacy_search("beauty") == taxonomy.expand("beauty")
    assert taxonomy.legacy_search("nope") == []


def test_round_trips_to_what_older_clients_send():
    for s in SERVICES:
        pair = taxonomy.to_legacy(s["key"])
        if pair:
            assert taxonomy.from_legacy(*pair) == s["key"], s["key"]
    assert taxonomy.to_legacy("qawwali") is None  # new; older clients can't show it


def test_every_bundle_slot_today_has_a_home():
    for slot, spec in CHATBOT_SLOTS.items():
        key = taxonomy.bundle_slot(slot)
        assert key, slot
        if spec["subcategory"]:
            assert taxonomy.expand(key) == [taxonomy.from_legacy(spec["category"], spec["subcategory"])]
    assert set(taxonomy.expand(taxonomy.bundle_slot("floral_decor"))) >= {"floral", "decor"}


def test_get_taxonomy_serves_every_picker_and_caches():
    r = client.get("/taxonomy")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [g["key"] for g in body["groups"]][:3] == ["venues", "planning", "food_drink"]
    venue = body["groups"][0]["services"][0]
    assert venue["key"] == "venue" and venue["popular"] == 1 and "capacity" in venue["attributes"]
    assert sum(len(g["services"]) for g in body["groups"]) == 73
    cuisine = body["attributes"]["cuisine"]
    assert cuisine["type"] == "multi" and {"key": "south_indian", "label": "South Indian"} in cuisine["options"]
    for a in body["attributes"].values():
        keys = [o["key"] for o in a.get("options", [])]
        assert len(keys) == len(set(keys)), a["key"]

    etag = r.headers["etag"]
    assert etag == f'"{body["version"]}"'
    again = client.get("/taxonomy", headers={"If-None-Match": etag})
    assert again.status_code == 304
    # Today's list is untouched for older clients.
    assert client.get("/vendors/categories").status_code == 200
