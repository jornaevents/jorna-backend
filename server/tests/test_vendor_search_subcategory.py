"""Vendor search narrows to a subcategory so a slot lists only that specialty.

Regression for the bundle-swap gap where a DJ slot also listed Dhol services
(both are music_entertainment) because search filtered by category only.
"""
import uuid

from app.db.models import User, Vendor, Service
from tests.test_api import TestingSessionLocal, client


def _seed_music_vendors():
    db = TestingSessionLocal()
    uid = uuid.uuid4().hex[:8]

    def make(subcat: str, label: str) -> str:
        u = User(
            email=f"vss_{subcat}_{uid}@t.com", username=f"vss_{subcat}_{uid}",
            password="pw", phone="1", f_name=label, l_name="V",
            age=30, location="New Jersey", gender="M", language="EN", token_version=0,
        )
        db.add(u); db.commit(); db.refresh(u)
        v = Vendor(user_id=u.user_id, bio="b", rating=4.7, num_events=5,
                   category="music_entertainment", subcategory=subcat)
        db.add(v); db.commit(); db.refresh(v)
        s = Service(name=label, price=1000.0, duration_minutes=120,
                    vendor_id=v.vendor_id, experience="e",
                    category="music_entertainment", subcategory=subcat)
        db.add(s); db.commit()
        return v.vendor_id

    dj_id = make("dj", "DJ Set")
    dhol_id = make("dhol", "Dhol Player")
    db.close()
    return dj_id, dhol_id


def test_subcategory_narrows_to_that_specialty():
    dj_id, dhol_id = _seed_music_vendors()

    resp = client.get("/vendors/search?category=music_entertainment&subcategory=dj")
    assert resp.status_code == 200
    vendor_ids = {it["vendor_id"] for it in resp.json()["items"]}

    assert dj_id in vendor_ids
    assert dhol_id not in vendor_ids  # the whole point: no Dhol in a DJ slot


def test_category_only_still_returns_both_subcategories():
    dj_id, dhol_id = _seed_music_vendors()

    resp = client.get("/vendors/search?category=music_entertainment")
    assert resp.status_code == 200
    vendor_ids = {it["vendor_id"] for it in resp.json()["items"]}

    assert dj_id in vendor_ids
    assert dhol_id in vendor_ids  # no subcategory filter → both show
