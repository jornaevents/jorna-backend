"""Phase 1 of "optional escrow": a vendor can opt into a manual Venmo/Zelle
payment track instead of Stripe. This only covers the settings surface —
PATCH /vendors/me saving/validating the new fields, and every existing
vendor defaulting to "stripe" so nothing behaves differently until they
opt in. Nothing here changes booking/charge behavior yet.
"""

import uuid

from app.db.models import User
from tests.test_api import TestingSessionLocal, client, make_auth_headers


def _register_vendor(prefix: str) -> dict:
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


def test_new_vendor_defaults_to_stripe():
    """POST /vendors' response is a minimal creation receipt (matches the
    existing instagram_username precedent, which is also absent from it) —
    the default is checked through GET /vendors/me instead."""
    headers = _register_vendor("pm_default")
    create_resp = client.post("/vendors", json={"bio": "bio"}, headers=headers)
    assert create_resp.status_code == 200, create_resp.text

    resp = client.get("/vendors/me", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["payment_method"] == "stripe"
    assert resp.json()["venmo_handle"] is None
    assert resp.json()["zelle_contact"] is None


def test_update_saves_manual_payment_method_and_handles():
    headers = _register_vendor("pm_manual")
    client.post("/vendors", json={"bio": "bio"}, headers=headers)
    resp = client.patch(
        "/vendors/me",
        json={"payment_method": "manual", "venmo_handle": "@some-vendor", "zelle_contact": "vendor@example.com"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["payment_method"] == "manual"
    assert body["venmo_handle"] == "@some-vendor"
    assert body["zelle_contact"] == "vendor@example.com"


def test_update_rejects_invalid_payment_method():
    headers = _register_vendor("pm_invalid")
    client.post("/vendors", json={"bio": "bio"}, headers=headers)
    resp = client.patch("/vendors/me", json={"payment_method": "paypal"}, headers=headers)
    assert resp.status_code == 400, resp.text


def test_update_trims_and_nulls_empty_handles():
    headers = _register_vendor("pm_trim")
    client.post("/vendors", json={"bio": "bio"}, headers=headers)
    client.patch("/vendors/me", json={"venmo_handle": "  @spaced  "}, headers=headers)
    resp = client.patch("/vendors/me", json={"zelle_contact": ""}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["venmo_handle"] == "@spaced"
    assert body["zelle_contact"] is None


def test_reload_shows_saved_payment_details():
    headers = _register_vendor("pm_reload")
    client.post("/vendors", json={"bio": "bio"}, headers=headers)
    client.patch(
        "/vendors/me",
        json={"payment_method": "manual", "venmo_handle": "@handle"},
        headers=headers,
    )
    resp = client.get("/vendors/me", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["payment_method"] == "manual"
    assert resp.json()["venmo_handle"] == "@handle"
