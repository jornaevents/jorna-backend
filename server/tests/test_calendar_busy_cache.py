"""The Google Calendar busy-block cache, its push-notification channel, and
the sweeps that keep both warm.

get_vendor_availability used to call Google's freebusy API live on every
request; now it reads a per-vendor cache, refreshed by watch_calendar's
push channel, the periodic sweeps, or (only when the cache doesn't cover
what was asked) a live fallback scoped to that one request.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import User, Vendor
from app.services import calendar_service as cs
from tests.test_api import TestingSessionLocal, client


def _vendor(*, connected: bool = True, synced: bool = False, cache=None):
    db = TestingSessionLocal()
    uid = uuid.uuid4().hex[:8]

    user = User(
        email=f"cal_cache_{uid}@test.com", username=f"cal_cache_{uid}", password="pw",
        phone="1", f_name="V", l_name="Endor", age=30, location="1",
        gender="F", language="EN", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    vendor = Vendor(user_id=user.user_id, bio="bio", rating=5.0, num_events=1)
    if connected:
        vendor.google_access_token = "tok"
        vendor.google_refresh_token = "refresh"
        vendor.calendar_id = "primary"
    if synced:
        vendor.google_busy_synced_at = datetime.now(timezone.utc)
        vendor.google_busy_cache = cache or []
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return db, vendor


def _mock_freebusy(mocker, busy):
    service = mocker.MagicMock()
    creds = mocker.MagicMock()
    creds.token = "tok"
    mocker.patch(
        "app.services.calendar_service.create_google_calendar_service",
        return_value=(service, creds),
    )
    mocker.patch("app.services.calendar_service.get_freebusy_schedule", return_value=busy)
    return service


class TestRefreshBusyCache:
    def test_populates_the_cache(self, mocker):
        db, vendor = _vendor()
        _mock_freebusy(mocker, [{"start": "2026-06-01T18:00:00Z", "end": "2026-06-01T22:00:00Z"}])

        assert cs.refresh_busy_cache(vendor, db) is True
        db.refresh(vendor)
        assert vendor.google_busy_cache == [
            {"start": "2026-06-01T18:00:00Z", "end": "2026-06-01T22:00:00Z"}
        ]
        assert vendor.google_busy_synced_at is not None

    def test_no_op_for_a_disconnected_vendor(self, mocker):
        db, vendor = _vendor(connected=False)
        mock = mocker.patch("app.services.calendar_service.create_google_calendar_service")

        assert cs.refresh_busy_cache(vendor, db) is False
        mock.assert_not_called()

    def test_a_google_failure_is_swallowed(self, mocker):
        db, vendor = _vendor()
        mocker.patch(
            "app.services.calendar_service.create_google_calendar_service",
            side_effect=RuntimeError("boom"),
        )
        assert cs.refresh_busy_cache(vendor, db) is False


class TestWatchCalendar:
    def test_stores_the_channel(self, mocker):
        db, vendor = _vendor()
        events = mocker.MagicMock()
        events.watch.return_value.execute.return_value = {
            "resourceId": "res_123",
            "expiration": str(int((datetime.now(timezone.utc) + timedelta(days=7)).timestamp() * 1000)),
        }
        service = mocker.MagicMock()
        service.events.return_value = events
        creds = mocker.MagicMock()
        creds.token = "tok"
        mocker.patch(
            "app.services.calendar_service.create_google_calendar_service",
            return_value=(service, creds),
        )

        assert cs.watch_calendar(vendor, db) is True
        db.refresh(vendor)
        assert vendor.google_channel_id
        assert vendor.google_channel_resource_id == "res_123"
        assert vendor.google_channel_expires_at is not None

    def test_no_op_for_a_disconnected_vendor(self, mocker):
        db, vendor = _vendor(connected=False)
        assert cs.watch_calendar(vendor, db) is False


class TestWebhook:
    def test_an_unknown_channel_does_nothing(self, mocker):
        mock = mocker.patch("app.services.calendar_service.refresh_busy_cache")
        db = TestingSessionLocal()
        cs.handle_calendar_webhook(channel_id="not-a-real-channel", db=db)
        mock.assert_not_called()

    def test_a_known_channel_refreshes_that_vendor(self, mocker):
        db, vendor = _vendor()
        vendor.google_channel_id = "chan_abc"
        db.commit()
        mock = mocker.patch("app.services.calendar_service.refresh_busy_cache")

        cs.handle_calendar_webhook(channel_id="chan_abc", db=db)

        mock.assert_called_once()
        assert mock.call_args.args[0].vendor_id == vendor.vendor_id

    def test_the_endpoint_always_200s_for_an_unknown_channel(self):
        r = client.post(
            "/vendors/google-calendar/webhook",
            headers={"X-Goog-Channel-ID": "nope", "X-Goog-Resource-State": "exists"},
        )
        assert r.status_code == 200

    def test_the_sync_confirmation_does_not_trigger_a_fetch(self, mocker):
        db, vendor = _vendor()
        vendor.google_channel_id = "chan_sync_test"
        db.commit()
        mock = mocker.patch("app.services.calendar_service.refresh_busy_cache")

        client.post(
            "/vendors/google-calendar/webhook",
            headers={"X-Goog-Channel-ID": "chan_sync_test", "X-Goog-Resource-State": "sync"},
        )

        mock.assert_not_called()


class TestSweeps:
    def test_renew_only_touches_channels_expiring_soon(self, mocker):
        db, soon = _vendor()
        soon.google_channel_id = "chan_soon"
        soon.google_channel_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        db.commit()

        later_db, later = _vendor()
        later.google_channel_id = "chan_later"
        later.google_channel_expires_at = datetime.now(timezone.utc) + timedelta(days=5)
        later_db.commit()

        watched = mocker.patch("app.services.calendar_service.watch_calendar", return_value=True)

        cs.renew_expiring_channels(db=db)

        renewed_ids = {c.args[0].vendor_id for c in watched.call_args_list}
        assert soon.vendor_id in renewed_ids
        assert later.vendor_id not in renewed_ids

    def test_resync_covers_every_connected_vendor(self, mocker):
        db, connected = _vendor()
        _, disconnected = _vendor(connected=False)

        refreshed = mocker.patch(
            "app.services.calendar_service.refresh_busy_cache", return_value=True
        )

        n = cs.resync_all_connected_vendors(db=db)

        refreshed_ids = {c.args[0].vendor_id for c in refreshed.call_args_list}
        assert connected.vendor_id in refreshed_ids
        assert disconnected.vendor_id not in refreshed_ids
        assert n == len(refreshed_ids)


class TestAvailabilityReadsTheCache:
    def test_within_the_cached_window_no_live_call_is_made(self, mocker):
        db, vendor = _vendor(
            synced=True,
            cache=[{"start": "2026-06-01T18:00:00+00:00", "end": "2026-06-01T22:00:00+00:00"}],
        )
        live = mocker.patch("app.services.calendar_service.create_google_calendar_service")

        result = cs.get_vendor_availability(
            vendor_id=vendor.vendor_id, start_date="2026-06-01", end_date="2026-06-02", db=db
        )

        live.assert_not_called()
        assert result["google_busy_times"] == [
            {"start": "2026-06-01T18:00:00+00:00", "end": "2026-06-01T22:00:00+00:00"}
        ]

    def test_a_request_past_the_cached_window_falls_back_to_a_live_fetch(self, mocker):
        db, vendor = _vendor(synced=True, cache=[])
        # Force the cache to look stale-for-this-request by asking past the
        # window it could possibly cover — the cache has nothing for this
        # date at all, so only a live fetch could produce a result.
        far = (datetime.now(timezone.utc) + timedelta(days=cs.BUSY_CACHE_DAYS_AHEAD + 30)).date()
        _mock_freebusy(mocker, [{"start": f"{far}T18:00:00Z", "end": f"{far}T22:00:00Z"}])

        result = cs.get_vendor_availability(
            vendor_id=vendor.vendor_id,
            start_date=str(far),
            end_date=str(far),
            db=db,
        )

        assert len(result["google_busy_times"]) == 1

    def test_never_synced_falls_back_to_a_live_fetch(self, mocker):
        db, vendor = _vendor(synced=False)
        _mock_freebusy(mocker, [{"start": "2026-06-01T18:00:00Z", "end": "2026-06-01T22:00:00Z"}])

        result = cs.get_vendor_availability(
            vendor_id=vendor.vendor_id, start_date="2026-06-01", end_date="2026-06-02", db=db
        )

        assert len(result["google_busy_times"]) == 1
