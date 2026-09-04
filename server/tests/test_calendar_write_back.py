"""Jorna -> the vendor's own Google Calendar.

sync_booking_to_calendar / remove_booking_from_calendar are best-effort by
the same contract as every other best-effort write in this codebase — a
Google failure must never surface as a failure of the booking action that
triggered it, so most of this asserts on Booking.google_event_id rather
than on anything raising.
"""

import uuid

import pytest

from app.db.models import Booking, Service, User, Vendor
from app.services import calendar_service as cs
from tests.test_api import TestingSessionLocal


def _world(*, write_scope: bool):
    db = TestingSessionLocal()
    uid = uuid.uuid4().hex[:8]

    user = User(
        email=f"cal_wb_{uid}@test.com", username=f"cal_wb_{uid}", password="pw",
        phone="1", f_name="V", l_name="Endor", age=30, location="1",
        gender="F", language="EN", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    vendor = Vendor(
        user_id=user.user_id, bio="bio", rating=5.0, num_events=1,
        google_access_token="tok", google_refresh_token="refresh",
        calendar_id="primary",
        google_granted_scopes=(
            "https://www.googleapis.com/auth/calendar.readonly"
            + (" https://www.googleapis.com/auth/calendar.events" if write_scope else "")
        ),
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    service = Service(
        name="Photography", price=500.0, duration_minutes=120,
        vendor_id=vendor.vendor_id, experience="5 years",
    )
    db.add(service)
    db.commit()
    db.refresh(service)

    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        date_iso="2026-06-01", time_start="18:00", time_end="22:00",
        location="123 Main St", status="approved",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    return {"db": db, "user": user, "vendor": vendor, "service": service, "booking": booking}


def _mock_gcal(mocker, *, event_id="evt_123"):
    """Patches create_google_calendar_service to return a fake events API."""
    events = mocker.MagicMock()
    events.insert.return_value.execute.return_value = {"id": event_id}
    events.update.return_value.execute.return_value = {"id": event_id}
    events.delete.return_value.execute.return_value = {}

    service = mocker.MagicMock()
    service.events.return_value = events

    creds = mocker.MagicMock()
    creds.token = "tok"  # unchanged — no refresh happened

    mocker.patch(
        "app.services.calendar_service.create_google_calendar_service",
        return_value=(service, creds),
    )
    return events


class TestSyncBookingToCalendar:
    def test_creates_an_event_for_a_write_enabled_vendor(self, mocker):
        world = _world(write_scope=True)
        events = _mock_gcal(mocker)

        cs.sync_booking_to_calendar(world["booking"], world["db"])

        events.insert.assert_called_once()
        assert events.update.call_count == 0
        world["db"].refresh(world["booking"])
        assert world["booking"].google_event_id == "evt_123"

    def test_updates_instead_of_recreating_once_an_event_exists(self, mocker):
        world = _world(write_scope=True)
        world["booking"].google_event_id = "already_there"
        world["db"].commit()
        events = _mock_gcal(mocker, event_id="already_there")

        cs.sync_booking_to_calendar(world["booking"], world["db"])

        events.update.assert_called_once()
        assert events.insert.call_count == 0

    def test_no_op_for_a_disconnected_vendor(self, mocker):
        world = _world(write_scope=True)
        world["vendor"].google_access_token = None
        world["db"].commit()
        events = _mock_gcal(mocker)

        cs.sync_booking_to_calendar(world["booking"], world["db"])

        events.insert.assert_not_called()

    def test_no_op_for_a_read_only_grant(self, mocker):
        world = _world(write_scope=False)
        events = _mock_gcal(mocker)

        cs.sync_booking_to_calendar(world["booking"], world["db"])

        events.insert.assert_not_called()
        world["db"].refresh(world["booking"])
        assert world["booking"].google_event_id is None

    def test_no_op_for_a_booking_with_no_real_hours(self, mocker):
        world = _world(write_scope=True)
        world["booking"].time_start = "TBD"
        world["booking"].time_end = "TBD"
        world["db"].commit()
        events = _mock_gcal(mocker)

        cs.sync_booking_to_calendar(world["booking"], world["db"])

        events.insert.assert_not_called()

    def test_a_google_failure_is_swallowed(self, mocker):
        world = _world(write_scope=True)
        mocker.patch(
            "app.services.calendar_service.create_google_calendar_service",
            side_effect=RuntimeError("boom"),
        )
        # Must not raise.
        cs.sync_booking_to_calendar(world["booking"], world["db"])


class TestRemoveBookingFromCalendar:
    def test_deletes_and_clears_the_event_id(self, mocker):
        world = _world(write_scope=True)
        world["booking"].google_event_id = "evt_123"
        world["db"].commit()
        events = _mock_gcal(mocker)

        cs.remove_booking_from_calendar(world["booking"], world["db"])

        events.delete.assert_called_once()
        world["db"].refresh(world["booking"])
        assert world["booking"].google_event_id is None

    def test_no_op_when_there_was_never_an_event(self, mocker):
        world = _world(write_scope=True)
        events = _mock_gcal(mocker)

        cs.remove_booking_from_calendar(world["booking"], world["db"])

        events.delete.assert_not_called()

    def test_an_already_deleted_event_is_not_an_error(self, mocker):
        from googleapiclient.errors import HttpError

        world = _world(write_scope=True)
        world["booking"].google_event_id = "evt_123"
        world["db"].commit()
        events = _mock_gcal(mocker)
        resp = mocker.MagicMock(status=404)
        events.delete.return_value.execute.side_effect = HttpError(resp, b"not found")

        cs.remove_booking_from_calendar(world["booking"], world["db"])

        world["db"].refresh(world["booking"])
        assert world["booking"].google_event_id is None

    def test_a_disconnected_vendor_just_clears_the_id(self, mocker):
        world = _world(write_scope=True)
        world["booking"].google_event_id = "evt_123"
        world["vendor"].google_access_token = None
        world["db"].commit()
        events = _mock_gcal(mocker)

        cs.remove_booking_from_calendar(world["booking"], world["db"])

        events.delete.assert_not_called()
        world["db"].refresh(world["booking"])
        assert world["booking"].google_event_id is None


class TestBookingStatusHooks:
    def test_approving_a_booking_creates_its_calendar_event(self, mocker):
        world = _world(write_scope=True)
        events = _mock_gcal(mocker)
        world["booking"].status = "pending"
        world["db"].commit()

        from app.services.booking_service import update_booking_status
        from app.models.schemas import BookingStatus

        update_booking_status(
            booking_id=world["booking"].booking_id,
            caller_user_id=world["vendor"].user_id,
            status=BookingStatus.APPROVED,
            db=world["db"],
        )

        events.insert.assert_called_once()

    def test_cancelling_an_approved_booking_removes_its_event(self, mocker):
        world = _world(write_scope=True)
        world["booking"].google_event_id = "evt_123"
        world["db"].commit()
        events = _mock_gcal(mocker)

        from app.services.booking_service import update_booking_status
        from app.models.schemas import BookingStatus

        update_booking_status(
            booking_id=world["booking"].booking_id,
            caller_user_id=world["vendor"].user_id,
            status=BookingStatus.REJECTED,
            db=world["db"],
        )

        events.delete.assert_called_once()

    def test_declining_a_never_approved_request_does_not_touch_the_calendar(self, mocker):
        world = _world(write_scope=True)
        world["booking"].status = "pending"
        world["db"].commit()
        events = _mock_gcal(mocker)

        from app.services.booking_service import update_booking_status
        from app.models.schemas import BookingStatus

        update_booking_status(
            booking_id=world["booking"].booking_id,
            caller_user_id=world["vendor"].user_id,
            status=BookingStatus.REJECTED,
            db=world["db"],
        )

        events.delete.assert_not_called()
        events.insert.assert_not_called()
