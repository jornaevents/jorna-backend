"""What a service detail page needs in order to exist.

The page shows one listing: its photos, its price, its description, who offers
it, and its reviews. Two of those came back missing.

`GET /services/{id}` returned the service and nothing about the vendor, so a
page that has to print "Anita Rosewood Events" under the title had to make a
second call for two strings that live one join away.

`/vendors/search` returned a row per vendor+service pair naming the service but
never saying which service it was, so a card could display a listing and had no
way to link to it.
"""
import uuid

from app.db.models import User, Vendor, Service
from tests.test_api import TestingSessionLocal, client


def _seed(**service_kwargs) -> dict:
    db = TestingSessionLocal()
    uid = uuid.uuid4().hex[:8]

    u = User(
        email=f"sd_{uid}@t.com", username=f"sd_{uid}",
        password="pw", phone="1", f_name="Anita", l_name="Rosewood",
        age=41, location="Mississauga, ON", gender="F", language="EN",
        token_version=0, pfp_url="https://example.test/anita.jpg",
    )
    db.add(u); db.commit(); db.refresh(u)

    v = Vendor(user_id=u.user_id, bio="Two acres of manicured grounds.",
               rating=4.8, num_events=23, category="venue")
    db.add(v); db.commit(); db.refresh(v)

    kwargs = dict(
        name="Outdoor Garden Venue", price=7500.0, vendor_id=v.vendor_id,
        experience="14+ years", category="venue",
    )
    kwargs.update(service_kwargs)
    s = Service(**kwargs)
    db.add(s); db.commit(); db.refresh(s)

    out = {"user": u, "vendor": v, "service_id": s.service_id,
           "vendor_id": v.vendor_id}
    db.close()
    return out


# ── The listing knows who is offering it ───────────────────────────────────


def test_a_service_names_its_vendor():
    seeded = _seed()

    r = client.get(f"/services/{seeded['service_id']}")
    assert r.status_code == 200
    body = r.json()

    assert body["vendor_name"] == "Anita Rosewood"
    assert body["vendor_rating"] == 4.8
    assert body["vendor_pfp_url"] == "https://example.test/anita.jpg"


def test_the_single_and_list_routes_agree_on_shape():
    """A caller holding a service from either route reads the same keys. They
    drifted apart once already, which is how the page ended up needing two
    calls for one listing."""
    seeded = _seed()

    one = client.get(f"/services/{seeded['service_id']}").json()
    listed = client.get(f"/services?vendor_id={seeded['vendor_id']}").json()["items"]
    mine = next(i for i in listed if i["service_id"] == seeded["service_id"])

    assert set(one) == set(mine)
    assert one == mine


def test_the_listing_still_carries_its_own_rating():
    """The fields added when reviews moved to services survive the join."""
    seeded = _seed()

    body = client.get(f"/services/{seeded['service_id']}").json()
    assert body["rating"] == 0.0
    assert body["num_reviews"] == 0


def test_an_unknown_service_is_still_a_404():
    r = client.get(f"/services/{uuid.uuid4()}")
    assert r.status_code == 404


def test_a_listing_whose_vendor_row_is_gone_still_loads():
    """An outer join, deliberately. A listing with no vendor row is broken data,
    but a page that 500s tells the client nothing and hides the listing too."""
    seeded = _seed()
    db = TestingSessionLocal()
    try:
        db.query(Vendor).filter(Vendor.vendor_id == seeded["vendor_id"]).delete()
        db.commit()
    finally:
        db.close()

    r = client.get(f"/services/{seeded['service_id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Outdoor Garden Venue"
    assert body["vendor_name"] is None
    assert body["vendor_rating"] is None


# ── A search result can be linked to ───────────────────────────────────────


def test_a_search_row_names_which_service_it_is():
    seeded = _seed()

    r = client.get("/vendors/search?category=venue")
    assert r.status_code == 200
    rows = [i for i in r.json()["items"] if i["vendor_id"] == seeded["vendor_id"]]

    assert rows, "seeded venue vendor missing from search"
    assert rows[0]["service_id"] == seeded["service_id"]
    assert rows[0]["service_name"] == "Outdoor Garden Venue"


def test_two_listings_from_one_vendor_are_separately_addressable():
    """The reason the id matters: a vendor with two listings produces two rows
    that differ only by service, and a card needs to link to its own."""
    seeded = _seed()
    db = TestingSessionLocal()
    try:
        second = Service(name="Indoor Banquet Hall", price=5200.0,
                         vendor_id=seeded["vendor_id"], experience="14+ years",
                         category="venue")
        db.add(second); db.commit(); db.refresh(second)
        second_id = second.service_id
    finally:
        db.close()

    r = client.get("/vendors/search?category=venue")
    rows = [i for i in r.json()["items"] if i["vendor_id"] == seeded["vendor_id"]]

    by_name = {i["service_name"]: i["service_id"] for i in rows}
    assert by_name["Outdoor Garden Venue"] == seeded["service_id"]
    assert by_name["Indoor Banquet Hall"] == second_id
