"""Tests for Sentry error-monitoring setup (app/observability.py)."""

import app.observability as obs
from app.observability import init_sentry, _scrub


def test_init_sentry_is_noop_without_dsn(monkeypatch):
    """No DSN → disabled, returns False, never touches the SDK."""
    monkeypatch.setattr(obs, "SENTRY_DSN", "")
    assert init_sentry() is False


def test_init_sentry_configures_sdk_when_dsn_set(monkeypatch):
    """With a DSN, sentry_sdk.init is called with safe defaults."""
    monkeypatch.setattr(obs, "SENTRY_DSN", "https://abc123@o1.ingest.sentry.io/1")
    monkeypatch.setattr(obs, "SENTRY_ENVIRONMENT", "test")

    captured = {}

    import sentry_sdk

    def fake_init(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(sentry_sdk, "init", fake_init)

    assert init_sentry() is True
    assert captured["dsn"] == "https://abc123@o1.ingest.sentry.io/1"
    assert captured["environment"] == "test"
    # PII off, and our header scrubber is wired in.
    assert captured["send_default_pii"] is False
    assert captured["before_send"] is _scrub


def test_scrub_strips_auth_headers_and_cookies():
    event = {
        "request": {
            "headers": {
                "Authorization": "Bearer supersecret",
                "Cookie": "session=abc",
                "Accept": "application/json",
            },
            "cookies": {"session": "abc"},
        }
    }
    out = _scrub(event, {})
    headers = out["request"]["headers"]
    assert headers["Authorization"] == "[Filtered]"
    assert headers["Cookie"] == "[Filtered]"
    # Non-sensitive headers are preserved.
    assert headers["Accept"] == "application/json"
    # Cookies dropped entirely.
    assert "cookies" not in out["request"]


def test_scrub_tolerates_missing_request():
    # Events without a request section (e.g. background errors) pass through.
    assert _scrub({}, {}) == {}
