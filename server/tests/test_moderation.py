"""Tests for content reporting and user blocking."""

import uuid
import pytest
from app.db.models import ContentReport, User, UserBlock
from tests.test_api import TestingSessionLocal, client, make_auth_headers


@pytest.fixture
def two_users():
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]
    a = User(
        email=f"mod_a_{uid}@test.com", username=f"mod_a_{uid}", password="pw",
        phone="1", f_name="Alice", l_name="A", age=25, location="NJ",
        gender="F", language="EN", token_version=0,
    )
    b = User(
        email=f"mod_b_{uid}@test.com", username=f"mod_b_{uid}", password="pw",
        phone="1", f_name="Bob", l_name="B", age=30, location="NY",
        gender="M", language="EN", token_version=0,
    )
    db.add_all([a, b])
    db.commit()
    db.refresh(a)
    db.refresh(b)
    yield {"a": a, "b": b, "db": db}
    db.close()


def test_create_report(two_users):
    a, b = two_users["a"], two_users["b"]
    db = two_users["db"]

    resp = client.post("/reports", json={
        "target_type": "user",
        "target_id": b.user_id,
        "reason": "harassment",
        "details": "Sent abusive messages",
    }, headers=make_auth_headers(a))
    assert resp.status_code == 201
    report_id = resp.json()["report_id"]

    row = db.query(ContentReport).filter(ContentReport.report_id == report_id).first()
    assert row is not None
    assert row.reporter_user_id == a.user_id
    assert row.status == "open"


def test_report_rejects_bad_enum(two_users):
    a = two_users["a"]
    resp = client.post("/reports", json={
        "target_type": "galaxy", "target_id": "x", "reason": "spam",
    }, headers=make_auth_headers(a))
    assert resp.status_code == 422

    resp = client.post("/reports", json={
        "target_type": "user", "target_id": "x", "reason": "didnt-like-it",
    }, headers=make_auth_headers(a))
    assert resp.status_code == 422


def test_report_requires_auth(two_users):
    resp = client.post("/reports", json={
        "target_type": "user", "target_id": "x", "reason": "spam",
    })
    assert resp.status_code in (401, 403)


def test_block_unblock_flow(two_users):
    a, b = two_users["a"], two_users["b"]

    # Block
    resp = client.post(f"/blocks/{b.user_id}", headers=make_auth_headers(a))
    assert resp.status_code == 201

    # Idempotent re-block
    resp = client.post(f"/blocks/{b.user_id}", headers=make_auth_headers(a))
    assert resp.status_code == 201
    assert resp.json()["message"] == "Already blocked"

    # Listed
    resp = client.get("/blocks", headers=make_auth_headers(a))
    assert resp.status_code == 200
    entries = resp.json()
    assert any(e["blocked_user_id"] == b.user_id for e in entries)
    assert any(e["blocked_name"] == "Bob B" for e in entries)

    # B's list is unaffected (blocks are one-directional)
    resp = client.get("/blocks", headers=make_auth_headers(b))
    assert all(e["blocked_user_id"] != a.user_id for e in resp.json())

    # Unblock
    resp = client.delete(f"/blocks/{b.user_id}", headers=make_auth_headers(a))
    assert resp.status_code == 200
    resp = client.get("/blocks", headers=make_auth_headers(a))
    assert all(e["blocked_user_id"] != b.user_id for e in resp.json())

    # Unblocking again 404s
    resp = client.delete(f"/blocks/{b.user_id}", headers=make_auth_headers(a))
    assert resp.status_code == 404


def test_cannot_block_self_or_missing(two_users):
    a = two_users["a"]
    resp = client.post(f"/blocks/{a.user_id}", headers=make_auth_headers(a))
    assert resp.status_code == 400
    resp = client.post("/blocks/does-not-exist", headers=make_auth_headers(a))
    assert resp.status_code == 404


def test_admin_reports_listing(two_users):
    a, b = two_users["a"], two_users["b"]
    db = two_users["db"]

    client.post("/reports", json={
        "target_type": "vendor", "target_id": "v1", "reason": "scam",
    }, headers=make_auth_headers(a))

    # Non-admin is rejected
    resp = client.get("/admin/reports", headers=make_auth_headers(a))
    assert resp.status_code == 403

    a.is_admin = True
    db.commit()
    resp = client.get("/admin/reports?status=open", headers=make_auth_headers(a))
    assert resp.status_code == 200
    reports = resp.json()
    assert any(r["target_id"] == "v1" and r["reason"] == "scam" for r in reports)

    # Mark reviewed
    rid = next(r["report_id"] for r in reports if r["target_id"] == "v1")
    resp = client.patch(f"/admin/reports/{rid}?status=reviewed", headers=make_auth_headers(a))
    assert resp.status_code == 200
