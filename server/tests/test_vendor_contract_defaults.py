"""Vendor.default_* contract-defaults fields (deposit %, cancellation window,
overtime/addon rate, contract_terms, guest_count_mode) round-tripping through
PATCH/GET /vendors/me. These are pure seed values for the frontend Contracts
builder (see app/db/models.py's Vendor comment) — nothing here is consumed by
backend logic, but they still have to survive a write/read cycle. Regression
test for a bug where they were accepted by neither the request schema nor the
service-layer update allowlist, and excluded from the response dict, so a
PATCH silently no-opped.
"""

import uuid

import pytest

from app.db.models import User, Vendor
from tests.test_api import TestingSessionLocal, client, make_auth_headers

_created = {"users": [], "vendors": []}


@pytest.fixture(autouse=True)
def _isolate():
    yield
    db = TestingSessionLocal()
    if _created["vendors"]:
        db.query(Vendor).filter(Vendor.vendor_id.in_(_created["vendors"])).delete(synchronize_session=False)
    if _created["users"]:
        db.query(User).filter(User.user_id.in_(_created["users"])).delete(synchronize_session=False)
    db.commit()
    db.close()
    for v in _created.values():
        v.clear()


def _setup_vendor():
    uid = uuid.uuid4().hex[:8]
    db = TestingSessionLocal()
    vendor_user = User(
        email=f"cd_v_{uid}@test.com", username=f"cd_v_{uid}", password="pw",
        phone="1", f_name="V", l_name="N", age=30, location="NJ",
        gender="F", language="EN", token_version=0,
    )
    db.add(vendor_user); db.commit(); db.refresh(vendor_user)
    vendor = Vendor(
        user_id=vendor_user.user_id, bio="b", category="venue", rating=0.0, num_events=0,
        payment_method="manual", venmo_handle="v",
    )
    db.add(vendor); db.commit(); db.refresh(vendor)
    headers = make_auth_headers(vendor_user)
    _created["users"].append(vendor_user.user_id)
    _created["vendors"].append(vendor.vendor_id)
    db.close()
    return headers


def test_contract_defaults_round_trip_through_patch_and_get():
    headers = _setup_vendor()

    patched = client.patch(
        "/vendors/me",
        json={
            "default_deposit_percent": 40,
            "default_cancellation_window_hours": 168,
            "default_overtime_rate_cents": 15_000,
            "default_addon_rate_cents": 5_000,
            "default_contract_terms": {"equipment_power": "Vendor brings all gear"},
            "default_guest_count_mode": "optional",
        },
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["default_deposit_percent"] == 40

    fetched = client.get("/vendors/me", headers=headers)
    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    assert body["default_deposit_percent"] == 40
    assert body["default_cancellation_window_hours"] == 168
    assert body["default_overtime_rate_cents"] == 15_000
    assert body["default_addon_rate_cents"] == 5_000
    assert body["default_contract_terms"] == {"equipment_power": "Vendor brings all gear"}
    assert body["default_guest_count_mode"] == "optional"


def test_contract_defaults_absent_by_default():
    headers = _setup_vendor()
    fetched = client.get("/vendors/me", headers=headers)
    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    assert body["default_deposit_percent"] is None
    assert body["default_contract_terms"] is None
