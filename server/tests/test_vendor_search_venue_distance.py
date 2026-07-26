"""Vendor search places a venue by where the building stands.

Regression for the browse gap that matched the bundle builder's: search judged
every category by the vendor's *User* coordinates against their
travel_radius_miles, so a venue in another state was offered whenever its owner
happened to live near the event — or had open_to_long_distance set, which
waived the distance check outright. A building does not travel.

Covers both paths through the location filter: the coordinate one, and the
`state`-string fallback used when the caller has no coordinates.
"""
import uuid

from app.db.models import User, Vendor, Service
from tests.test_api import TestingSessionLocal, client

# The event, and two venues: one across town, one ~190 miles away.
EVENT_LAT, EVENT_LNG = 40.7128, -74.0060      # New York, NY
NEAR_LAT, NEAR_LNG = 40.7357, -74.1724        # Newark, NJ — ~10 mi
FAR_LAT, FAR_LNG = 42.3601, -71.0589          # Boston, MA — ~190 mi


def _seed_venues() -> tuple[str, str, str]:
    """A near and a far venue, both owned by vendors living *at* the event and
    both open to long distance — so only the venue's own coordinates can tell
    them apart. Returns (tag, near_vendor_id, far_vendor_id)."""
    db = TestingSessionLocal()
    tag = uuid.uuid4().hex[:8]

    def make(label: str, vlat: float, vlng: float, address: str) -> str:
        u = User(
            email=f"vsvd_{label}_{tag}@t.com", username=f"vsvd_{label}_{tag}",
            password="pw", phone="1", f_name=label.title(), l_name="Hall",
            age=30, location="New Jersey", gender="F", language="EN",
            token_version=0,
            # Owner sits at the event either way.
            latitude=EVENT_LAT, longitude=EVENT_LNG,
        )
        db.add(u); db.commit(); db.refresh(u)
        v = Vendor(
            user_id=u.user_id, bio="b", rating=4.7, num_events=5,
            category="venue", subcategory=None,
            travel_radius_miles=30, open_to_long_distance=True,
        )
        db.add(v); db.commit(); db.refresh(v)
        s = Service(
            name=f"vsvd-{tag}-{label}", price=1000.0, vendor_id=v.vendor_id,
            experience="e", category="venue", subcategory=None,
            location=address, venue_latitude=vlat, venue_longitude=vlng,
        )
        db.add(s); db.commit()
        return v.vendor_id

    near_id = make("near", NEAR_LAT, NEAR_LNG, "1 Market St, Newark, NJ")
    far_id = make("far", FAR_LAT, FAR_LNG, "500 Boylston St, Boston, MA")
    db.close()
    return tag, near_id, far_id


def _search(**params) -> list[dict]:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    resp = client.get(f"/vendors/search?{query}")
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def test_far_venue_is_excluded_by_its_own_location():
    tag, near_id, far_id = _seed_venues()

    items = _search(service_name=f"vsvd-{tag}", latitude=EVENT_LAT, longitude=EVENT_LNG)
    vendor_ids = {it["vendor_id"] for it in items}

    assert near_id in vendor_ids
    # Excluded despite its owner living at the event and open_to_long_distance.
    assert far_id not in vendor_ids


def test_reported_distance_is_to_the_venue_not_the_owner():
    """The owner is at the event (0 mi); the venue is ~10 mi away. Showing 0
    would mean the number on the card describes the wrong place."""
    tag, near_id, _far_id = _seed_venues()

    items = _search(service_name=f"vsvd-{tag}", latitude=EVENT_LAT, longitude=EVENT_LNG)
    near = next(it for it in items if it["vendor_id"] == near_id)

    assert 5 < near["distance_miles"] < 15


def test_state_fallback_still_goes_by_the_owner_documented_gap():
    """Pins the *unfixed* half, so the gap is visible rather than assumed shut.

    With no coordinates there is nothing to measure, and the filter falls back to
    a state string matched against the owner's location — so a Boston venue whose
    owner lives in New Jersey is still returned for a New Jersey search. Matching
    the venue's own address instead was tried and reverted: it is free-text, and
    a two-letter code substring-matches inside ordinary words ("MA" in "1 Market
    St"). Closing this needs a structured state on the service.

    Change this test when that lands; don't delete it.
    """
    tag, near_id, far_id = _seed_venues()

    # Both owners live in New Jersey; only one of the two venues is there.
    nj_ids = {it["vendor_id"] for it in _search(service_name=f"vsvd-{tag}", state="New%20Jersey")}
    assert near_id in nj_ids
    assert far_id in nj_ids  # the gap: matched on its owner, not the building


def test_traveling_vendor_still_uses_travel_radius():
    """The non-venue path is untouched — a DJ based far away but open to long
    distance is still offered, which is the rule a venue must not inherit."""
    db = TestingSessionLocal()
    tag = uuid.uuid4().hex[:8]
    u = User(
        email=f"vsvd_dj_{tag}@t.com", username=f"vsvd_dj_{tag}", password="pw",
        phone="1", f_name="Far", l_name="DJ", age=30, location="Massachusetts",
        gender="F", language="EN", token_version=0,
        latitude=FAR_LAT, longitude=FAR_LNG,
    )
    db.add(u); db.commit(); db.refresh(u)
    v = Vendor(user_id=u.user_id, bio="b", rating=4.7, num_events=5,
               category="music_entertainment", subcategory="dj",
               travel_radius_miles=30, open_to_long_distance=True)
    db.add(v); db.commit(); db.refresh(v)
    s = Service(name=f"vsvd-dj-{tag}", price=1000.0, vendor_id=v.vendor_id,
                experience="e", category="music_entertainment", subcategory="dj")
    db.add(s); db.commit()
    vendor_id = v.vendor_id
    db.close()

    items = _search(service_name=f"vsvd-dj-{tag}", latitude=EVENT_LAT, longitude=EVENT_LNG)
    assert vendor_id in {it["vendor_id"] for it in items}


def test_venue_without_coordinates_is_excluded():
    """Unknown distance is exactly what this filter exists to exclude."""
    db = TestingSessionLocal()
    tag = uuid.uuid4().hex[:8]
    u = User(
        email=f"vsvd_nc_{tag}@t.com", username=f"vsvd_nc_{tag}", password="pw",
        phone="1", f_name="No", l_name="Coords", age=30, location="New York",
        gender="F", language="EN", token_version=0,
        latitude=EVENT_LAT, longitude=EVENT_LNG,
    )
    db.add(u); db.commit(); db.refresh(u)
    v = Vendor(user_id=u.user_id, bio="b", rating=4.7, num_events=5,
               category="venue", subcategory=None)
    db.add(v); db.commit(); db.refresh(v)
    s = Service(name=f"vsvd-nc-{tag}", price=1000.0, vendor_id=v.vendor_id,
                experience="e", category="venue", subcategory=None)
    db.add(s); db.commit()
    vendor_id = v.vendor_id
    db.close()

    items = _search(service_name=f"vsvd-nc-{tag}", latitude=EVENT_LAT, longitude=EVENT_LNG)
    assert vendor_id not in {it["vendor_id"] for it in items}
