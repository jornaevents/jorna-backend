import pytest
from app.db.models import Vendor, VendorAvailability, Booking, User, Service
from app.services.calendar_service import _encode_state
from tests.test_api import TestingSessionLocal, client, make_auth_headers


def _make_state(vendor_id: str) -> str:
    """Generate a valid HMAC-signed OAuth state for a vendor."""
    return _encode_state(vendor_id, "test-code-verifier")


def test_google_auth_callback_success(mocker):
    db = TestingSessionLocal()

    user = User(
        email="oauth@test.com", username="oauth_test", password="pw",
        phone="1", f_name="A", l_name="B", age=20, location="123",
        gender="M", language="EN", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    vendor = Vendor(user_id=user.user_id, bio="bio", rating=5.0, num_events=1)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    vendor_id = vendor.vendor_id
    db.close()

    mock_flow = mocker.patch("app.services.calendar_service.get_google_auth_flow")
    mock_flow_instance = mocker.MagicMock()
    mock_flow.return_value = mock_flow_instance
    mock_flow_instance.credentials.token = "fake_access_token"
    mock_flow_instance.credentials.refresh_token = "fake_refresh_token"

    state = _make_state(vendor_id)
    # Don't follow the redirect — the callback redirects to the frontend
    response = client.get(
        f"/vendors/auth/callback?state={state}&code=auth_code_123",
        follow_redirects=False,
    )
    assert response.status_code in (302, 307)
    assert "success=true" in response.headers["location"]

    db2 = TestingSessionLocal()
    vendor_fresh = db2.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    assert vendor_fresh.google_access_token == "fake_access_token"
    assert vendor_fresh.google_refresh_token == "fake_refresh_token"
    db2.close()


def test_google_auth_callback_invalid_state():
    """Forged/malformed state is rejected — redirects to frontend with success=false."""
    response = client.get(
        "/vendors/auth/callback?state=not-a-valid-state&code=auth_code_123",
        follow_redirects=False,
    )
    assert response.status_code in (302, 307)
    assert "success=false" in response.headers["location"]


def test_google_auth_callback_flow_error(mocker):
    db = TestingSessionLocal()
    vendor = db.query(Vendor).first()
    vendor_id = vendor.vendor_id
    db.close()

    mock_flow = mocker.patch("app.services.calendar_service.get_google_auth_flow")
    mock_flow_instance = mocker.MagicMock()
    mock_flow_instance.fetch_token.side_effect = Exception("Invalid grant")
    mock_flow.return_value = mock_flow_instance

    state = _make_state(vendor_id)
    response = client.get(
        f"/vendors/auth/callback?state={state}&code=bad_code",
        follow_redirects=False,
    )
    assert response.status_code in (302, 307)
    assert "success=false" in response.headers["location"]


def test_calendar_availability_aggregation(mocker):
    db = TestingSessionLocal()

    user = User(
        email="avail@test.com", username="avail_test", password="pw",
        phone="1", f_name="A", l_name="B", age=20, location="123",
        gender="M", language="EN", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    vendor = Vendor(
        user_id=user.user_id, bio="bio", rating=5.0, num_events=1,
        google_access_token="test_token", google_refresh_token="test_refresh",
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    baseline = VendorAvailability(vendor_id=vendor.vendor_id, day_of_week=0, start_time="09:00", end_time="17:00")
    db.add(baseline)
    db.commit()

    service = Service(name="Test", price=100.0, duration_minutes=60, vendor_id=vendor.vendor_id, experience="none")
    db.add(service)
    db.commit()
    db.refresh(service)

    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="10:00", time_end="11:30",
        location="123 Main St", date_iso="2026-03-02",
        # Approved: only a booking that commits the vendor makes them busy. A
        # pending request is a lead, and one they declined isn't a booking at
        # all — both used to mark them unavailable and hide them from search.
        status="approved",
    )
    db.add(booking)
    # A second request the vendor never answered, and one they turned down.
    # Neither belongs in their busy times.
    db.add(Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="14:00", time_end="16:00",
        location="123 Main St", date_iso="2026-03-02", status="pending",
    ))
    db.add(Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="18:00", time_end="20:00",
        location="123 Main St", date_iso="2026-03-02", status="rejected",
    ))
    db.commit()
    vendor_id = vendor.vendor_id
    db.close()

    mock_creds = mocker.MagicMock()
    mock_creds.token = "test_token"
    mocker.patch("app.services.calendar_service.create_google_calendar_service", return_value=("mock_service", mock_creds))
    mock_google_api = mocker.patch("app.services.calendar_service.get_freebusy_schedule")
    mock_google_api.return_value = [{"start": "2026-03-02T13:00:00Z", "end": "2026-03-02T14:00:00Z"}]

    response = client.get(f"/vendors/{vendor_id}/availability?start_date=2026-03-01T00:00:00Z&end_date=2026-03-07T23:59:59Z")
    assert response.status_code == 200
    data = response.json()

    assert "0" in data["baseline_hours_map"]
    assert data["baseline_hours_map"]["0"] == ["09:00", "17:00"]
    assert len(data["internal_busy_times"]) == 1
    assert data["internal_busy_times"][0]["start"] == "2026-03-02T10:00:00+00:00"
    assert len(data["google_busy_times"]) == 1
    assert data["google_busy_times"][0]["start"] == "2026-03-02T13:00:00+00:00"


def test_calendar_availability_vendor_not_found():
    response = client.get("/vendors/invalid-uid/availability?start_date=2026-03-01T00:00:00Z&end_date=2026-03-07T23:59:59Z")
    assert response.status_code == 404
    assert response.json()["detail"] == "Vendor not found"


def test_check_in_no_coordinates():
    db = TestingSessionLocal()
    user = db.query(User).first()
    vendor = db.query(Vendor).first()
    service = db.query(Service).first()

    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="10:00", time_end="11:30",
        location="123 Main St", date_iso="2026-03-02",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    user_id = user.user_id
    user_email = user.email
    booking_id = booking.booking_id
    db.close()

    from tests.test_api import make_auth_headers_from_parts
    headers = make_auth_headers_from_parts(user_id, user_email, 0)
    response = client.post(
        f"/bookings/{booking_id}/check-in",
        json={"latitude": 40.0, "longitude": -70.0},
        headers=headers,
    )
    assert response.status_code == 400
    # A booked venue is no longer the only way to have a place: an event with a
    # geocoded address of its own can be checked into too, so the refusal is
    # about not having either.
    assert response.json()["detail"] == "This event has no address on it yet — check-in opens once the plan has a place to be."


def test_check_in_unauthorized_user():
    """A user who is neither the customer nor the vendor gets 403."""
    db = TestingSessionLocal()
    user = db.query(User).first()
    vendor = db.query(Vendor).first()
    service = db.query(Service).first()

    booking = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id, service_id=service.service_id,
        time_start="10:00", time_end="11:30",
        location="145 Main St", date_iso="2026-03-02",
        venue_latitude=40.0, venue_longitude=-70.0,
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    booking_id = booking.booking_id
    db.close()

    # Create a completely unrelated user and use their token
    from tests.test_api import make_auth_headers_from_parts
    import uuid
    headers = make_auth_headers_from_parts(str(uuid.uuid4()), "stranger@test.com", 0)
    response = client.post(
        f"/bookings/{booking_id}/check-in",
        json={"latitude": 40.0, "longitude": -70.0},
        headers=headers,
    )
    # get_current_user returns 401 (user not in DB), or 403 if booking unauthorized
    assert response.status_code in (401, 403)


# ── Who may link a calendar, and where the flow lands ──────────────────


def _vendor_with_owner(email: str):
    """A fresh vendor and auth headers for the account behind it."""
    from tests.test_api import make_auth_headers_from_parts

    db = TestingSessionLocal()
    user = User(
        email=email, username=email.split("@")[0], password="pw",
        phone="1", f_name="A", l_name="B", age=30, location="123",
        gender="M", language="EN", token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    vendor = Vendor(user_id=user.user_id, bio="bio", rating=5.0, num_events=1)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    vendor_id, user_id, user_email = vendor.vendor_id, user.user_id, user.email
    db.close()
    return vendor_id, make_auth_headers_from_parts(user_id, user_email, 0)


def test_google_auth_requires_a_signed_in_caller():
    """Anonymous callers can't start a link — the URL they'd get is a valid,
    signed authorization for somebody else's vendor row."""
    vendor_id, _ = _vendor_with_owner("cal_owner1@test.com")
    assert client.get(f"/vendors/{vendor_id}/google-auth").status_code in (401, 403)


def test_google_auth_refuses_another_vendor():
    """Signed in is not enough: completing the flow writes tokens onto the
    vendor named in the state, so it must be the caller's own."""
    victim_id, _ = _vendor_with_owner("cal_victim@test.com")
    _, attacker_headers = _vendor_with_owner("cal_attacker@test.com")
    r = client.get(f"/vendors/{victim_id}/google-auth", headers=attacker_headers)
    assert r.status_code == 403


def test_calendar_status_is_not_public():
    """Which accounts a vendor has linked isn't a browsing client's business."""
    vendor_id, headers = _vendor_with_owner("cal_status@test.com")
    assert client.get(f"/vendors/{vendor_id}/calendar-status").status_code in (401, 403)

    _, other = _vendor_with_owner("cal_status_other@test.com")
    assert client.get(f"/vendors/{vendor_id}/calendar-status", headers=other).status_code == 403

    ok = client.get(f"/vendors/{vendor_id}/calendar-status", headers=headers)
    assert ok.status_code == 200
    assert ok.json() == {"google_calendar_connected": False}


def test_callback_returns_a_browser_to_the_web_app(mocker):
    """A browser has no app to bounce into, so client=web lands in the web app."""
    from app.config import WEB_APP_URL

    vendor_id, _ = _vendor_with_owner("cal_web@test.com")
    flow = mocker.patch("app.services.calendar_service.get_google_auth_flow")
    flow.return_value.credentials.token = "tok"
    flow.return_value.credentials.refresh_token = "refresh"

    state = _encode_state(vendor_id, "test-code-verifier", "web")
    r = client.get(
        f"/vendors/auth/callback?state={state}&code=abc", follow_redirects=False
    )
    assert r.status_code in (302, 307)
    location = r.headers["location"]
    assert location.startswith(f"{WEB_APP_URL}/calendar-connected/")
    assert "success=true" in location


def test_callback_still_returns_ios_to_the_bridge_page(mocker):
    """The app's route is unchanged, including for states issued before the
    client was recorded at all."""
    from app.config import FRONTEND_URL

    vendor_id, _ = _vendor_with_owner("cal_ios@test.com")
    flow = mocker.patch("app.services.calendar_service.get_google_auth_flow")
    flow.return_value.credentials.token = "tok"
    flow.return_value.credentials.refresh_token = "refresh"

    for state in (
        _encode_state(vendor_id, "test-code-verifier", "ios"),
        _encode_state(vendor_id, "test-code-verifier"),  # no client — an older state
    ):
        r = client.get(
            f"/vendors/auth/callback?state={state}&code=abc", follow_redirects=False
        )
        assert r.status_code in (302, 307)
        assert r.headers["location"].startswith(f"{FRONTEND_URL}/calendar-connected?")


def test_a_failed_web_link_comes_back_to_the_web_app(mocker):
    """The error has to reach the vendor where they are, not on a host they've
    never heard of."""
    from app.config import WEB_APP_URL

    vendor_id, _ = _vendor_with_owner("cal_web_fail@test.com")
    flow = mocker.patch("app.services.calendar_service.get_google_auth_flow")
    flow.return_value.fetch_token.side_effect = Exception("Invalid grant")

    state = _encode_state(vendor_id, "test-code-verifier", "web")
    r = client.get(
        f"/vendors/auth/callback?state={state}&code=bad", follow_redirects=False
    )
    location = r.headers["location"]
    assert location.startswith(f"{WEB_APP_URL}/calendar-connected/")
    assert "success=false" in location


def test_an_unreadable_state_still_redirects():
    """Nothing to read means nothing to route by, so it goes to the default and
    says what happened rather than throwing a 500 at Google."""
    r = client.get(
        "/vendors/auth/callback?state=garbage&code=abc", follow_redirects=False
    )
    assert r.status_code in (302, 307)
    assert "success=false" in r.headers["location"]


# ── Checking in at an address, with no venue booked through Jorna ──────
#
# A wedding in a family hall has a full address and no venue booking. Until the
# event carried its own pin, nobody in that plan could check in at all, so every
# vendor's payout waited on a confirmation there was no way to give.


def _plan_at(address_pin, *, venue_service_pin=None, booking_over=None):
    """A bundle, its event, and a booking — with or without a venue service."""
    import uuid
    from app.db.models import Bundle, Event
    from datetime import datetime, timezone
    from tests.test_api import make_auth_headers_from_parts

    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc)

    client_user = User(
        email=f"pin_c_{uid}@test.com", username=f"pin_c_{uid}", password="pw",
        phone="1", f_name="C", l_name="L", age=30, location="x",
        gender="M", language="EN", token_version=0,
    )
    vendor_user = User(
        email=f"pin_v_{uid}@test.com", username=f"pin_v_{uid}", password="pw",
        phone="1", f_name="V", l_name="N", age=30, location="x",
        gender="F", language="EN", token_version=0,
    )
    db.add_all([client_user, vendor_user])
    db.commit()
    db.refresh(client_user)
    db.refresh(vendor_user)

    vendor = Vendor(user_id=vendor_user.user_id, bio="b", rating=5.0, num_events=1)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    dj = Service(name="DJ", price=500.0, duration_minutes=180,
                 vendor_id=vendor.vendor_id, experience="5y")
    db.add(dj)
    db.commit()
    db.refresh(dj)

    event = Event(
        user_id=client_user.user_id, name="Hall Wedding", date_iso="2026-09-05",
        location="12 Maple Ave, Evanston, IL 60201", event_type="wedding",
        address_latitude=address_pin[0] if address_pin else None,
        address_longitude=address_pin[1] if address_pin else None,
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    bundle = Bundle(user_id=client_user.user_id, name="Plan", status="confirmed",
                    event_id=event.event_id, created_at=now, updated_at=now)
    db.add(bundle)
    db.commit()
    db.refresh(bundle)

    booking = Booking(
        user_id=client_user.user_id, vendor_id=vendor.vendor_id, service_id=dj.service_id,
        time_start="18:00", time_end="23:00", location="12 Maple Ave",
        date_iso="2026-09-05", status="approved", bundle_id=bundle.bundle_id,
        **(booking_over or {}),
    )
    db.add(booking)

    if venue_service_pin:
        venue_service = Service(
            name="Grand Hall", price=5000.0, duration_minutes=600,
            vendor_id=vendor.vendor_id, experience="10y", category="venue",
            location="99 Other St", venue_latitude=venue_service_pin[0],
            venue_longitude=venue_service_pin[1],
        )
        db.add(venue_service)
        db.commit()
        db.refresh(venue_service)
        db.add(Booking(
            user_id=client_user.user_id, vendor_id=vendor.vendor_id,
            service_id=venue_service.service_id, time_start="12:00", time_end="23:59",
            location="99 Other St", date_iso="2026-09-05", status="approved",
            bundle_id=bundle.bundle_id,
            venue_latitude=venue_service_pin[0], venue_longitude=venue_service_pin[1],
        ))

    db.commit()
    db.refresh(booking)
    out = {
        "booking_id": booking.booking_id,
        "headers": make_auth_headers_from_parts(
            client_user.user_id, client_user.email, 0
        ),
        "vendor_headers": make_auth_headers_from_parts(
            vendor_user.user_id, vendor_user.email, 0
        ),
    }
    db.close()
    return out


def test_check_in_uses_the_events_own_address_when_no_venue_is_booked():
    plan = _plan_at((42.0451, -87.6877))
    r = client.post(
        f"/bookings/{plan['booking_id']}/check-in",
        json={"latitude": 42.0451, "longitude": -87.6877},
        headers=plan["headers"],
    )
    assert r.status_code == 200, r.text


def test_the_address_pin_is_still_a_place_you_have_to_be():
    """It's a fallback for where, not a way around being there."""
    plan = _plan_at((42.0451, -87.6877))
    r = client.post(
        f"/bookings/{plan['booking_id']}/check-in",
        json={"latitude": 41.8781, "longitude": -87.6298},  # Chicago, ~12 miles
        headers=plan["headers"],
    )
    assert r.status_code == 400
    assert "at the venue to check in" in r.json()["detail"]


def test_a_booked_venue_still_outranks_the_typed_address():
    """The venue is the source of truth where there is one — otherwise removing
    it would stop mattering, which is what the clearing exists to prevent."""
    plan = _plan_at((42.0451, -87.6877), venue_service_pin=(41.8781, -87.6298))

    # At the typed address, but the plan's venue is elsewhere: refused.
    at_address = client.post(
        f"/bookings/{plan['booking_id']}/check-in",
        json={"latitude": 42.0451, "longitude": -87.6877},
        headers=plan["headers"],
    )
    assert at_address.status_code == 400

    # At the booked venue: fine.
    at_venue = client.post(
        f"/bookings/{plan['booking_id']}/check-in",
        json={"latitude": 41.8781, "longitude": -87.6298},
        headers=plan["headers"],
    )
    assert at_venue.status_code == 200, at_venue.text


def test_no_venue_and_no_address_says_so():
    plan = _plan_at(None)
    r = client.post(
        f"/bookings/{plan['booking_id']}/check-in",
        json={"latitude": 42.0451, "longitude": -87.6877},
        headers=plan["headers"],
    )
    assert r.status_code == 400
    assert "no address on it yet" in r.json()["detail"]


def test_the_payload_names_the_point_check_in_will_use():
    """Clients gate their button on checkin_latitude, so it has to resolve the
    same way check_in does — otherwise the button appears for a call the server
    refuses, or hides for one it would allow."""
    plan = _plan_at((42.0451, -87.6877))
    r = client.get(f"/bookings/{plan['booking_id']}", headers=plan["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    # No venue booked, so the booking's own venue pin is empty...
    assert body["venue_latitude"] is None
    # ...but the plan still has a place to be.
    assert body["checkin_latitude"] == 42.0451
    assert body["checkin_longitude"] == -87.6877


def test_a_booked_venue_is_the_point_the_payload_names():
    plan = _plan_at((42.0451, -87.6877), venue_service_pin=(41.8781, -87.6298))
    r = client.get(f"/bookings/{plan['booking_id']}", headers=plan["headers"])
    assert r.status_code == 200
    assert r.json()["checkin_latitude"] == 41.8781


def test_no_place_at_all_names_no_point():
    plan = _plan_at(None)
    r = client.get(f"/bookings/{plan['booking_id']}", headers=plan["headers"])
    assert r.status_code == 200
    assert r.json()["checkin_latitude"] is None


def test_checking_in_confirms_the_vendor_even_before_payment(mocker):
    """A vendor at the venue has done the thing being attested. Whether the
    client's payment has cleared is somebody else's timing, and gating on it
    left the confirmation permanently unrecorded — the client would confirm and
    then wait on a second confirmation that could never arrive."""
    plan = _plan_at((42.0451, -87.6877), booking_over={"payment_status": "unpaid"})
    mocker.patch("app.utils.notifications.notify_check_in", return_value={})

    r = client.post(
        f"/bookings/{plan['booking_id']}/check-in",
        json={"latitude": 42.0451, "longitude": -87.6877},
        headers=plan["vendor_headers"],
    )
    assert r.status_code == 200, r.text

    db = TestingSessionLocal()
    fresh = db.query(Booking).filter(Booking.booking_id == plan["booking_id"]).first()
    assert fresh.vendor_checked_in_at is not None
    assert fresh.vendor_confirmed_at is not None
    # Nothing was paid, so nothing was released.
    assert fresh.payment_status == "unpaid"
    db.close()
