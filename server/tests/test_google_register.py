"""One-tap Google sign-up: POST /auth/google/register.

The Supabase token is normally verified against Supabase's JWKS over the network,
so every test here monkeypatches the decode and hands the service the claims a
real Google identity would carry.

The lookup endpoint's behaviour is pinned too. Auto-creating inside *it* is what
broke sign-up in April (69faa5c) for clients that post a registration form
afterwards — iOS still does — so a test guards that it keeps creating nothing.
"""

import uuid

import pytest

from app.limiter import limiter
from app.services import auth_service
from tests.test_api import client, TestingSessionLocal
from app.db.models import User


@pytest.fixture(autouse=True)
def without_rate_limits():
    """The endpoint allows 5/minute and this file calls it more than that, all from
    one client address. Left on, the tests would also start failing based on what
    ran before them, since the quota is shared across the suite."""
    limiter.enabled = False
    yield
    limiter.enabled = True


def fake_claims(email: str, *, sub: str | None = None, full_name: str | None = "Asha Mehta"):
    meta = {"avatar_url": "https://example.test/a.png"}
    if full_name is not None:
        meta["full_name"] = full_name
    return {
        "sub": sub or str(uuid.uuid4()),
        "email": email,
        "user_metadata": meta,
    }


@pytest.fixture
def google_identity(monkeypatch):
    """Patch the Supabase decode to return whatever claims a test sets up."""

    box = {}

    def _decode(access_token: str):
        return box["claims"]

    monkeypatch.setattr(auth_service, "_decode_supabase_access_token", _decode)
    return box


def test_google_register_creates_the_whole_account(google_identity):
    email = f"asha-{uuid.uuid4().hex[:8]}@example.test"
    google_identity["claims"] = fake_claims(email)

    res = client.post("/auth/google/register", json={"access_token": "fake"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["is_new_user"] is True
    assert body["access_token"] and body["refresh_token"]

    # The token alone was enough: no form, no password.
    me = client.get("/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200, me.text
    profile = me.json()
    assert profile["email"] == email
    assert profile["f_name"] == "Asha"
    assert profile["l_name"] == "Mehta"
    assert profile["username"].startswith("asha")
    assert profile["has_password"] is False
    assert profile["pfp_url"] == "https://example.test/a.png"
    # Left for complete_profile, which is what it exists for.
    assert profile["age"] is None
    assert profile["location"] is None

    db = TestingSessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        assert user.password is None
    finally:
        db.close()


def test_google_register_is_idempotent(google_identity):
    """A double tap or a retry after a dropped response must not fail."""
    email = f"twice-{uuid.uuid4().hex[:8]}@example.test"
    google_identity["claims"] = fake_claims(email)

    first = client.post("/auth/google/register", json={"access_token": "fake"})
    second = client.post("/auth/google/register", json={"access_token": "fake"})

    assert first.status_code == 200 and second.status_code == 200, second.text
    assert first.json()["is_new_user"] is True
    assert second.json()["is_new_user"] is False
    assert second.json()["access_token"]
    assert second.json()["user_id"] == first.json()["user_id"]


def test_google_register_handles_a_missing_name(google_identity):
    """f_name/l_name are NOT NULL, so a nameless Google account still has to work."""
    email = f"noname-{uuid.uuid4().hex[:8]}@example.test"
    google_identity["claims"] = fake_claims(email, full_name=None)

    res = client.post("/auth/google/register", json={"access_token": "fake"})
    assert res.status_code == 200, res.text
    me = client.get("/me", headers={"Authorization": f"Bearer {res.json()['access_token']}"})
    assert me.json()["f_name"] == email.split("@")[0]
    assert me.json()["l_name"] == "-"


def test_username_collision_gets_a_counter(google_identity):
    """Two Google accounts whose email prefixes match can't collide on username."""
    suffix = uuid.uuid4().hex[:8]
    google_identity["claims"] = fake_claims(f"same{suffix}@one.test")
    first = client.post("/auth/google/register", json={"access_token": "fake"})
    google_identity["claims"] = fake_claims(f"same{suffix}@two.test")
    second = client.post("/auth/google/register", json={"access_token": "fake"})

    assert first.status_code == 200 and second.status_code == 200, second.text
    names = {
        client.get("/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"}).json()[
            "username"
        ]
        for r in (first, second)
    }
    assert len(names) == 2


def test_password_login_against_a_google_only_account_401s(google_identity):
    """Must not 500 on `None.encode()`, and must not reveal that the email exists."""
    email = f"nopw-{uuid.uuid4().hex[:8]}@example.test"
    google_identity["claims"] = fake_claims(email)
    client.post("/auth/google/register", json={"access_token": "fake"})

    res = client.post("/auth/login", json={"identifier": email, "password": "Guess1234"})
    assert res.status_code == 401, res.text
    # Same wording as any other failed login, so it can't be used as an oracle.
    assert "Invalid credentials" in res.json()["detail"]


def test_change_password_points_a_google_only_account_at_reset(google_identity):
    email = f"chpw-{uuid.uuid4().hex[:8]}@example.test"
    google_identity["claims"] = fake_claims(email)
    created = client.post("/auth/google/register", json={"access_token": "fake"}).json()

    res = client.post(
        "/auth/change-password",
        json={"current_password": "whatever1A", "new_password": "NewPass123"},
        headers={"Authorization": f"Bearer {created['access_token']}"},
    )
    assert res.status_code == 400, res.text
    assert "no password yet" in res.json()["detail"]


def test_lookup_still_creates_nothing(google_identity):
    """Guards the April regression: iOS posts a register form after looking up."""
    email = f"lookup-{uuid.uuid4().hex[:8]}@example.test"
    google_identity["claims"] = fake_claims(email)

    res = client.post("/auth/google/lookup", json={"access_token": "fake"})
    assert res.status_code == 200, res.text
    assert res.json()["is_new_user"] is True
    assert res.json()["access_token"] is None

    db = TestingSessionLocal()
    try:
        assert db.query(User).filter(User.email == email).first() is None
    finally:
        db.close()
