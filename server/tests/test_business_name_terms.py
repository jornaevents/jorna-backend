"""A vendor's optional business name (0082) and the sign-up terms box.

The business name is what clients see the vendor called; without one the
account holder's own name stands in, as before. Terms acceptance is recorded
when the sign-up form sends accept_terms.
"""

import uuid

import pytest

from app.db.models import User, Vendor
from app.limiter import limiter
from app.services.names import vendor_display_name, vendor_user_display_name
from tests.test_api import TestingSessionLocal, client, make_auth_headers


def _vendor(prefix: str, **create) -> tuple[dict, str]:
    uid = uuid.uuid4().hex[:8]
    db = TestingSessionLocal()
    user = User(
        email=f"{prefix}_{uid}@test.com", username=f"{prefix}_{uid}", password="pw",
        f_name="Priya", l_name="Shah", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    headers = make_auth_headers(user)
    db.close()
    resp = client.post("/vendors", json={"bio": "bio", **create}, headers=headers)
    assert resp.status_code == 200, resp.text
    return headers, resp.json()["vendor_id"]


@pytest.fixture(autouse=True)
def without_rate_limits():
    """POST /auth/register allows 3/minute for the whole suite's one client
    address; left on, these fail depending on what ran before them (same fix
    as test_bookings.py's)."""
    limiter.enabled = False
    yield
    limiter.enabled = True


# ── Business name ─────────────────────────────────────────────────────


def test_a_vendor_can_be_created_with_a_business_name():
    headers, vendor_id = _vendor("bn_create", business_name="  Saffron   &  Silk ")
    me = client.get("/vendors/me", headers=headers).json()
    assert me["business_name"] == "Saffron & Silk", "trimmed, inner spaces collapsed"
    public = client.get(f"/vendors/{vendor_id}").json()
    assert public["business_name"] == "Saffron & Silk"
    assert public["f_name"] == "Priya", "the person's name is still there"


def test_it_is_optional_and_blank_clears_it():
    headers, vendor_id = _vendor("bn_clear")
    assert client.get("/vendors/me", headers=headers).json()["business_name"] is None

    resp = client.patch("/vendors/me", json={"business_name": "Saffron & Silk"}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["business_name"] == "Saffron & Silk"

    resp = client.patch("/vendors/me", json={"business_name": "   "}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["business_name"] is None


def test_it_is_capped_at_120_characters():
    headers, _ = _vendor("bn_long")
    resp = client.patch("/vendors/me", json={"business_name": "x" * 121}, headers=headers)
    assert resp.status_code == 422


def test_the_display_name_prefers_the_business_and_falls_back_to_the_person():
    person = User(f_name="Priya", l_name="Shah")
    assert vendor_display_name(Vendor(business_name="Saffron & Silk"), person) == "Saffron & Silk"
    assert vendor_display_name(Vendor(business_name=None), person) == "Priya Shah"
    assert vendor_display_name(Vendor(business_name="  "), person) == "Priya Shah"
    assert vendor_display_name(None, None, "your vendor") == "your vendor"


def test_the_lookup_by_user_finds_the_business_name():
    headers, vendor_id = _vendor("bn_lookup", business_name="Saffron & Silk")
    db = TestingSessionLocal()
    try:
        vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
        user = db.query(User).filter(User.user_id == vendor.user_id).first()
        assert vendor_user_display_name(user) == "Saffron & Silk"
        # A client has no vendor row: their own name.
        client_user = User(email=f"c_{uuid.uuid4().hex[:8]}@test.com", username=f"c_{uuid.uuid4().hex[:8]}",
                           password="pw", f_name="Ananya", l_name="Rao", token_version=0)
        db.add(client_user)
        db.commit()
        assert vendor_user_display_name(client_user) == "Ananya Rao"
    finally:
        db.close()


# ── Terms acceptance ──────────────────────────────────────────────────


def _register(**extra) -> User:
    uid = uuid.uuid4().hex[:8]
    resp = client.post("/auth/register", json={
        "email": f"terms_{uid}@test.com", "password": "Passw0rdOK",
        "f_name": "Priya", "l_name": "Shah", **extra,
    })
    assert resp.status_code == 200, resp.text
    db = TestingSessionLocal()
    try:
        return db.query(User).filter(User.email == f"terms_{uid}@test.com").first()
    finally:
        db.close()


def test_sign_up_records_when_the_terms_were_accepted():
    assert _register(accept_terms=True).terms_accepted_at is not None


def test_a_sign_up_without_the_box_still_works_and_records_nothing():
    # The iOS app doesn't show the box yet; it must still be able to register.
    assert _register().terms_accepted_at is None
