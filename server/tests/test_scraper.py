"""POST /admin/scraper/run — the weekly Instagram enrichment.

The cron job calling it timed out every week: the endpoint scraped every
linked vendor inside the request, each one a synchronous Apify call of up to
~2.5 minutes. It now answers 202 and runs in the background (wait=true keeps
the old inline behaviour), one run at a time.
"""

import logging
import uuid

import pytest

from app.db.models import Service, User, Vendor
from app.routers import admin
from tests.test_api import TestingSessionLocal, client, make_auth_headers

KEY = "test-scraper-key"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("APIFY_API_TOKEN", "apify-test")
    monkeypatch.setenv("SCRAPER_API_KEY", KEY)
    monkeypatch.setattr(admin, "_session_factory", TestingSessionLocal)
    monkeypatch.setattr(admin.time, "sleep", lambda _s: None)
    yield
    assert not admin._scraper_lock.locked(), "a run left the scraper lock held"


def _linked_vendor(db, *, bio=""):
    tag = uuid.uuid4().hex[:8]
    user = User(
        email=f"scr_{tag}@t.com", username=f"scr_{tag}", password="pw", phone="1",
        f_name="Scr", l_name="Aper", age=30, location="NJ", gender="F",
        language="EN", token_version=0,
    )
    db.add(user); db.commit(); db.refresh(user)
    vendor = Vendor(user_id=user.user_id, bio=bio, rating=0.0, num_events=0,
                    instagram_username=f"ig_{tag}")
    db.add(vendor); db.commit(); db.refresh(vendor)
    service = Service(name="Coverage", price=1000.0, duration_minutes=300,
                      vendor_id=vendor.vendor_id, experience="5y")
    db.add(service); db.commit()
    return vendor.vendor_id, vendor.instagram_username


def _fake_profile(username):
    return {
        "biography": "Bhangra and sangeet DJ in NJ",
        "posts": [
            {"caption": "Great night #wedding #bollywood", "displayUrl": f"https://img/{username}/1.jpg"},
            {"caption": "#sangeet vibes", "displayUrl": f"https://img/{username}/2.jpg"},
        ],
    }


@pytest.fixture
def apify_ok(monkeypatch):
    calls = []

    def fake(_token, username):
        calls.append(username)
        return _fake_profile(username), None

    monkeypatch.setattr(admin, "_scrape_profile", fake)
    return calls


def test_cron_call_answers_202_and_enriches_in_the_background(apify_ok, caplog):
    db = TestingSessionLocal()
    vendor_id, username = _linked_vendor(db)
    db.close()

    with caplog.at_level(logging.INFO, logger="app.routers.admin"):
        res = client.post("/admin/scraper/run", headers={"X-Scraper-Key": KEY})

    assert res.status_code == 202
    body = res.json()
    assert body["started"] is True and body["dry_run"] is False and body["vendors"] >= 1
    assert "results" not in body

    # TestClient runs background tasks before handing back the response.
    assert username in apify_ok
    db = TestingSessionLocal()
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    assert {"wedding", "bollywood", "sangeet", "bhangra"} <= set(vendor.instagram_tags)
    assert vendor.bio == "Bhangra and sangeet DJ in NJ"
    # Tags and bio only — the package's photos are never touched.
    assert db.query(Service).filter(Service.vendor_id == vendor_id).first().media is None
    db.close()
    assert any("Instagram scraper finished" in r.getMessage() for r in caplog.records)


def test_dry_run_in_the_background_writes_nothing(apify_ok):
    db = TestingSessionLocal()
    vendor_id, _ = _linked_vendor(db)
    db.close()

    res = client.post("/admin/scraper/run?dry_run=true", headers={"X-Scraper-Key": KEY})

    assert res.status_code == 202 and res.json()["dry_run"] is True
    db = TestingSessionLocal()
    assert db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first().instagram_tags is None
    db.close()


def test_wait_true_runs_inline_and_returns_per_vendor_results(apify_ok):
    db = TestingSessionLocal()
    vendor_id, username = _linked_vendor(db)
    db.close()

    res = client.post("/admin/scraper/run?wait=true&dry_run=true", headers={"X-Scraper-Key": KEY})

    assert res.status_code == 200
    mine = [r for r in res.json()["results"] if r["vendor_id"] == vendor_id]
    assert mine and mine[0]["status"] == "dry_run" and mine[0]["username"] == username


def test_a_second_call_during_a_run_gets_409(apify_ok):
    admin._scraper_lock.acquire()
    try:
        res = client.post("/admin/scraper/run", headers={"X-Scraper-Key": KEY})
        res_wait = client.post("/admin/scraper/run?wait=true", headers={"X-Scraper-Key": KEY})
    finally:
        admin._scraper_lock.release()

    assert res.status_code == 409 and res_wait.status_code == 409
    assert apify_ok == []


def test_failed_vendors_are_logged_and_the_next_run_can_start(monkeypatch, caplog):
    db = TestingSessionLocal()
    _, username = _linked_vendor(db)
    db.close()
    monkeypatch.setattr(admin, "_scrape_profile", lambda _t, _u: (None, "Apify returned HTTP 402: out of credit"))

    with caplog.at_level(logging.INFO, logger="app.routers.admin"):
        first = client.post("/admin/scraper/run", headers={"X-Scraper-Key": KEY})
    second = client.post("/admin/scraper/run?dry_run=true", headers={"X-Scraper-Key": KEY})

    assert first.status_code == 202 and second.status_code == 202
    assert any(f"@{username} failed: Apify returned HTTP 402" in r.getMessage() for r in caplog.records)


def test_a_crashing_run_still_releases_the_lock(monkeypatch, caplog):
    db = TestingSessionLocal()
    _linked_vendor(db)
    db.close()

    def boom(*_a, **_k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(admin, "_scrape_all", boom)
    with caplog.at_level(logging.ERROR, logger="app.routers.admin"):
        res = client.post("/admin/scraper/run", headers={"X-Scraper-Key": KEY})

    assert res.status_code == 202
    assert any("Instagram scraper run crashed" in r.getMessage() for r in caplog.records)


def test_missing_apify_token_is_a_400_before_anything_starts(monkeypatch, apify_ok):
    monkeypatch.delenv("APIFY_API_TOKEN")
    res = client.post("/admin/scraper/run", headers={"X-Scraper-Key": KEY})
    assert res.status_code == 400 and apify_ok == []


def test_wrong_key_is_rejected(apify_ok):
    res = client.post("/admin/scraper/run", headers={"X-Scraper-Key": "nope"})
    assert res.status_code == 401 and apify_ok == []


def test_the_key_in_the_query_string_is_no_longer_accepted(apify_ok):
    """It ended up in access logs and the cron service's run history."""
    res = client.post(f"/admin/scraper/run?api_key={KEY}")
    assert res.status_code == 401 and apify_ok == []


def test_an_admin_token_works_too(apify_ok):
    db = TestingSessionLocal()
    tag = uuid.uuid4().hex[:8]
    adm = User(
        email=f"scradm_{tag}@t.com", username=f"scradm_{tag}", password="pw", phone="1",
        f_name="A", l_name="D", age=30, location="NJ", gender="F",
        language="EN", token_version=0, is_admin=True,
    )
    db.add(adm); db.commit(); db.refresh(adm)
    headers = make_auth_headers(adm)
    db.close()

    res = client.post("/admin/scraper/run?dry_run=true", headers=headers)
    assert res.status_code == 202
