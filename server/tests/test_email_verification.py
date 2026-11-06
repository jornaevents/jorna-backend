"""An account proves its email with a code: POST /auth/email/send-code and
/auth/email/verify (docs/DECISIONS.md #44). Google sign-ins count as proved;
changing the email un-proves it."""

import re
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app import config
from app.db.models import EmailCode, User
from app.limiter import limiter
from app.services import auth_service
from tests.test_api import TestingSessionLocal, client, make_auth_headers

_created: list[str] = []


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _isolate():
    limiter.enabled = False
    yield
    limiter.enabled = True
    db = TestingSessionLocal()
    if _created:
        db.query(EmailCode).filter(EmailCode.user_id.in_(_created)).delete(synchronize_session=False)
        db.query(User).filter(User.user_id.in_(_created)).delete(synchronize_session=False)
        db.commit()
    db.close()
    _created.clear()


@pytest.fixture
def emails():
    with patch(
        "app.services.email_verification_service.send_email", return_value={"success": True, "id": "e1"},
    ) as sent:
        yield sent


def _user(**overrides) -> tuple[str, dict]:
    uid = uuid.uuid4().hex[:8]
    db = TestingSessionLocal()
    user = User(
        email=f"ev_{uid}@test.com", username=f"ev_{uid}", password="pw", f_name="Priya", l_name="Shah",
        token_version=0, **overrides,
    )
    db.add(user); db.commit(); db.refresh(user)
    _created.append(user.user_id)
    out = (user.user_id, make_auth_headers(user))
    db.close()
    return out


def _sent_code(emails) -> str:
    return re.search(r"(\d{6})", emails.call_args.kwargs["subject"]).group(1)


def _verified_at(user_id: str):
    db = TestingSessionLocal()
    try:
        return db.query(User).filter(User.user_id == user_id).one().email_verified_at
    finally:
        db.close()


def test_a_new_account_is_unverified():
    _, headers = _user()
    assert client.get("/me", headers=headers).json()["email_verified"] is False


def test_the_emailed_code_verifies_the_account(emails):
    user_id, headers = _user()
    res = client.post("/auth/email/send-code", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["email_verified"] is False
    assert body["sent_to"].startswith("e") and "@test.com" in body["sent_to"]
    assert emails.call_args.kwargs["to"].startswith("ev_")

    res = client.post("/auth/email/verify", json={"code": _sent_code(emails)}, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["email_verified"] is True
    assert _verified_at(user_id) is not None
    assert client.get("/me", headers=headers).json()["email_verified"] is True


def test_a_wrong_code_is_refused_and_five_retire_it(emails):
    user_id, headers = _user()
    client.post("/auth/email/send-code", headers=headers)
    right = _sent_code(emails)
    wrong = "000000" if right != "000000" else "111111"
    for _ in range(4):
        res = client.post("/auth/email/verify", json={"code": wrong}, headers=headers)
        assert res.status_code == 400
        assert "isn't right" in res.json()["detail"]
    res = client.post("/auth/email/verify", json={"code": wrong}, headers=headers)
    assert "Too many" in res.json()["detail"]
    # Even the right code is dead now.
    res = client.post("/auth/email/verify", json={"code": right}, headers=headers)
    assert res.status_code == 400
    assert _verified_at(user_id) is None


def test_a_new_code_retires_the_old_one(emails):
    _, headers = _user()
    client.post("/auth/email/send-code", headers=headers)
    first = _sent_code(emails)
    client.post("/auth/email/send-code", headers=headers)
    second = _sent_code(emails)
    if first != second:
        assert client.post("/auth/email/verify", json={"code": first}, headers=headers).status_code == 400
    assert client.post("/auth/email/verify", json={"code": second}, headers=headers).status_code == 200


def test_an_expired_code_is_refused(emails):
    user_id, headers = _user()
    client.post("/auth/email/send-code", headers=headers)
    db = TestingSessionLocal()
    db.query(EmailCode).filter(EmailCode.user_id == user_id).update(
        {EmailCode.expires_at: _now() - timedelta(minutes=1)}, synchronize_session=False,
    )
    db.commit(); db.close()
    res = client.post("/auth/email/verify", json={"code": _sent_code(emails)}, headers=headers)
    assert res.status_code == 400
    assert "expired" in res.json()["detail"]


def test_already_verified_sends_nothing(emails):
    _, headers = _user(email_verified_at=_now())
    res = client.post("/auth/email/send-code", headers=headers)
    assert res.json()["email_verified"] is True
    emails.assert_not_called()


def test_a_failed_send_is_an_error(emails):
    emails.return_value = {"success": False}
    user_id, headers = _user()
    with patch.object(config, "SIGNING_TEST_CODE", ""):
        res = client.post("/auth/email/send-code", headers=headers)
    assert res.status_code == 502
    db = TestingSessionLocal()
    assert db.query(EmailCode).filter(EmailCode.user_id == user_id).count() == 0
    db.close()


def test_the_staging_test_code_verifies(emails):
    user_id, headers = _user()
    with patch.object(config, "SIGNING_TEST_CODE", "424242"):
        res = client.post("/auth/email/verify", json={"code": "424242"}, headers=headers)
    assert res.status_code == 200
    assert _verified_at(user_id) is not None


def test_changing_the_email_unverifies(emails):
    user_id, headers = _user(email_verified_at=_now())
    new = f"ev_new_{uuid.uuid4().hex[:8]}@test.com"
    res = client.patch("/me", json={"email": new}, headers=headers)
    assert res.status_code == 200
    assert res.json()["email_verified"] is False
    assert _verified_at(user_id) is None


def test_saving_the_same_email_keeps_it_verified():
    user_id, headers = _user(email_verified_at=_now())
    email = client.get("/me", headers=headers).json()["email"]
    client.patch("/me", json={"email": email.upper()}, headers=headers)
    assert _verified_at(user_id) is not None


def test_a_code_sent_before_an_email_change_doesnt_verify(emails):
    user_id, headers = _user()
    client.post("/auth/email/send-code", headers=headers)
    code = _sent_code(emails)
    client.patch("/me", json={"email": f"ev_moved_{uuid.uuid4().hex[:8]}@test.com"}, headers=headers)
    res = client.post("/auth/email/verify", json={"code": code}, headers=headers)
    assert res.status_code == 400
    assert _verified_at(user_id) is None


def test_signed_out_is_401():
    assert client.post("/auth/email/send-code").status_code == 401
    assert client.post("/auth/email/verify", json={"code": "123456"}).status_code == 401


# ── Google ────────────────────────────────────────────────────────────


@pytest.fixture
def google(monkeypatch):
    box = {}
    monkeypatch.setattr(auth_service, "_decode_supabase_access_token", lambda token: box["claims"])
    return box


def test_google_sign_up_is_verified(google):
    email = f"ev_g_{uuid.uuid4().hex[:8]}@example.test"
    google["claims"] = {"sub": str(uuid.uuid4()), "email": email, "user_metadata": {"full_name": "Asha Mehta"}}
    res = client.post("/auth/google/register", json={"access_token": "fake"})
    assert res.status_code == 200, res.text
    _created.append(res.json()["user_id"])
    assert _verified_at(res.json()["user_id"]) is not None


def test_google_sign_in_verifies_a_password_account_with_that_email(google):
    user_id, _ = _user()
    db = TestingSessionLocal()
    email = db.query(User).filter(User.user_id == user_id).one().email
    db.close()
    google["claims"] = {"sub": str(uuid.uuid4()), "email": email, "user_metadata": {}}
    res = client.post("/auth/google/lookup", json={"access_token": "fake"})
    assert res.status_code == 200
    assert res.json()["user_id"] == user_id
    assert _verified_at(user_id) is not None
