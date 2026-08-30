"""specializations on POST /vendors and PATCH /vendors/me.

The frontend onboarding wizard lets a vendor pick every category+subcategory
they sell in, not just one, and has sent the full list as `specializations`
for a while — but until this column existed, nothing stored more than the
first entry (mirrored onto category/subcategory). A 2026-08-29 QA pass
against the live API confirmed a two-item POST /vendors came back with no
`specializations` key at all, and a reload showed only the first pick.
"""

import uuid

from app.db.models import User
from tests.test_api import TestingSessionLocal, client, make_auth_headers


def _register_vendor(prefix: str) -> dict:
    """A signed-in user with no vendor profile yet, made directly through the
    DB session — like test_google_auth_redirect and the vendor fixtures in
    test_when_vendors_are_told.py — so this file doesn't burn through
    /auth/register's shared rate limit just to get an authenticated caller."""
    uid = uuid.uuid4().hex[:8]
    db = TestingSessionLocal()
    user = User(
        email=f"{prefix}_{uid}@test.com", username=f"{prefix}_{uid}", password="pw",
        phone="1234567890", f_name="Test", l_name="Vendor", age=30, location="10001",
        gender="Test Gender", language="English", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    headers = make_auth_headers(user)
    db.close()
    return headers


def test_create_persists_and_returns_every_specialization():
    headers = _register_vendor("specs_create")
    resp = client.post(
        "/vendors",
        json={
            "bio": "QA test bio",
            "category": "music_entertainment",
            "subcategory": "dj",
            "specializations": [
                {"category": "music_entertainment", "subcategory": "dj"},
                {"category": "music_entertainment", "subcategory": "dhol"},
            ],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["specializations"] == [
        {"category": "music_entertainment", "subcategory": "dj"},
        {"category": "music_entertainment", "subcategory": "dhol"},
    ]
    # category/subcategory still mirror the first entry — search and the
    # older list endpoints filter on those columns, not specializations.
    assert body["category"] == "music_entertainment"
    assert body["subcategory"] == "dj"


def test_reload_shows_every_specialization_not_just_the_first():
    headers = _register_vendor("specs_reload")
    client.post(
        "/vendors",
        json={
            "bio": "QA test bio",
            "specializations": [
                {"category": "beauty", "subcategory": "bridal_makeup"},
                {"category": "beauty", "subcategory": "hair_stylist"},
            ],
        },
        headers=headers,
    )
    resp = client.get("/vendors/me", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["specializations"] == [
        {"category": "beauty", "subcategory": "bridal_makeup"},
        {"category": "beauty", "subcategory": "hair_stylist"},
    ]


def test_create_without_specializations_returns_empty_list():
    """No fallback reconstruction here — that's the frontend's job
    (vendorSpecializations() in types.ts) for rows that predate this column."""
    headers = _register_vendor("specs_none")
    resp = client.post("/vendors", json={"bio": "no specializations"}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["specializations"] == []


def test_update_replaces_the_full_list():
    headers = _register_vendor("specs_update")
    client.post(
        "/vendors",
        json={
            "bio": "start",
            "specializations": [{"category": "venue", "subcategory": None}],
        },
        headers=headers,
    )
    resp = client.patch(
        "/vendors/me",
        json={
            "specializations": [
                {"category": "catering", "subcategory": None},
                {"category": "bar_beverage", "subcategory": None},
            ]
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["specializations"] == [
        {"category": "catering", "subcategory": None},
        {"category": "bar_beverage", "subcategory": None},
    ]


def test_rejects_a_subcategory_that_does_not_belong_to_its_category():
    headers = _register_vendor("specs_invalid")
    resp = client.post(
        "/vendors",
        json={
            "bio": "bad pair",
            "specializations": [{"category": "music_entertainment", "subcategory": "bridal_makeup"}],
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


def test_rejects_more_than_twenty_specializations():
    headers = _register_vendor("specs_toomany")
    resp = client.post(
        "/vendors",
        json={
            "bio": "greedy",
            "specializations": [{"category": "venue", "subcategory": None} for _ in range(21)],
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
