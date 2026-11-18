"""Which side an account signed up on, and what /me says it is (0091).

An account is a vendor only when it has a vendors row. ``signup_side`` records
which sign-up made it, so the web apps can tell a client from a vendor who
signed up and left before onboarding's first step. Both come back on GET /me,
so an app knows what an account is as soon as it knows who's signed in.
"""

import uuid

import pytest

from app.db.models import User
from app.limiter import limiter
from tests.test_api import TestingSessionLocal, client
from tests.test_google_register import (  # noqa: F401 (fixture)
    fake_claims,
    google_identity,
)


@pytest.fixture(autouse=True)
def without_rate_limits():
    """/auth/register allows 3/minute for the whole suite's one client address."""
    limiter.enabled = False
    yield
    limiter.enabled = True


def _register(**extra) -> dict:
    """Sign up with a password; returns auth headers for the new account."""
    email = f"side_{uuid.uuid4().hex[:8]}@test.com"
    resp = client.post("/auth/register", json={
        "email": email, "password": "Passw0rdOK", "f_name": "Priya", "l_name": "Shah", **extra,
    })
    assert resp.status_code == 200, resp.text
    login = client.post("/auth/login", json={"identifier": email, "password": "Passw0rdOK"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _me(headers: dict) -> dict:
    resp = client.get("/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── Password sign-up ──────────────────────────────────────────────────


@pytest.mark.parametrize("side", ["client", "vendor"])
def test_sign_up_records_its_side(side):
    me = _me(_register(side=side))
    assert me["signup_side"] == side
    assert me["is_vendor"] is False, "signing up as a vendor doesn't make a vendor profile"


def test_a_sign_up_without_a_side_records_none():
    # iOS and older web builds don't send it.
    me = _me(_register())
    assert me["signup_side"] is None
    assert me["is_vendor"] is False


def test_an_unknown_side_is_refused():
    resp = client.post("/auth/register", json={
        "email": f"side_{uuid.uuid4().hex[:8]}@test.com", "password": "Passw0rdOK",
        "f_name": "Priya", "l_name": "Shah", "side": "admin",
    })
    assert resp.status_code == 422


# ── is_vendor ─────────────────────────────────────────────────────────


def test_me_says_vendor_once_onboarding_makes_the_profile():
    headers = _register(side="vendor")
    assert _me(headers)["is_vendor"] is False

    resp = client.post("/vendors", json={"bio": "bio"}, headers=headers)
    assert resp.status_code == 200, resp.text

    me = _me(headers)
    assert me["is_vendor"] is True
    assert me["signup_side"] == "vendor"


def test_patch_me_returns_the_same_fields():
    headers = _register(side="client")
    resp = client.patch("/me", json={"f_name": "Ananya"}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_vendor"] is False
    assert resp.json()["signup_side"] == "client"


# ── Google sign-up ────────────────────────────────────────────────────


def test_google_sign_up_records_its_side(google_identity):  # noqa: F811
    email = f"gside-{uuid.uuid4().hex[:8]}@example.test"
    google_identity["claims"] = fake_claims(email)

    res = client.post("/auth/google/register", json={"access_token": "fake", "side": "vendor"})
    assert res.status_code == 200, res.text
    me = _me({"Authorization": f"Bearer {res.json()['access_token']}"})
    assert me["signup_side"] == "vendor"
    assert me["is_vendor"] is False


def test_signing_in_again_with_google_never_changes_the_side(google_identity):  # noqa: F811
    email = f"gkeep-{uuid.uuid4().hex[:8]}@example.test"
    google_identity["claims"] = fake_claims(email)

    first = client.post("/auth/google/register", json={"access_token": "fake", "side": "client"})
    again = client.post("/auth/google/register", json={"access_token": "fake", "side": "vendor"})
    assert first.status_code == 200 and again.status_code == 200, again.text
    assert again.json()["is_new_user"] is False

    db = TestingSessionLocal()
    try:
        assert db.query(User).filter(User.email == email).first().signup_side == "client"
    finally:
        db.close()
