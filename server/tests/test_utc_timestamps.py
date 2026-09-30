"""Timestamps go out with their UTC offset (app/utils/timeutil.py) — a
naive ISO string is read as local time by browsers and iOS, which put the
contract timeline four hours off in New Jersey."""

from datetime import datetime, timedelta, timezone

from app.utils.timeutil import utc_iso
from tests.test_api import client
from tests.test_guest_booking_flow import (  # noqa: F401 — _isolate is an autouse fixture
    _create_contract,
    _created,
    _isolate,
    _setup_vendor,
)


def test_utc_iso_marks_naive_values_as_utc():
    assert utc_iso(datetime(2026, 9, 28, 14, 37)) == "2026-09-28T14:37:00+00:00"
    assert utc_iso("2026-09-28T14:37:00") == "2026-09-28T14:37:00+00:00"
    eastern = timezone(timedelta(hours=-4))
    assert utc_iso(datetime(2026, 9, 28, 10, 37, tzinfo=eastern)) == "2026-09-28T14:37:00+00:00"
    assert utc_iso(None) is None


def test_contract_and_timeline_times_carry_their_offset():
    v = _setup_vendor()
    c = _create_contract(v, date_iso="2027-10-20")
    for field in ("sent_at", "hold_expires_at"):
        assert c[field].endswith("+00:00"), (field, c[field])
    full = client.get(f"/contracts/{c['booking_id']}", headers=v["headers"]).json()
    assert all(e["at"].endswith("+00:00") for e in full["timeline"])
    page = client.get(f"/guest-bookings/{c['contract_token']}").json()
    assert page["hold_expires_at"].endswith("+00:00")
