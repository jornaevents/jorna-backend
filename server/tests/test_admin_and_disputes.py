"""Tests for admin system and dispute handling."""

import uuid
import pytest
from datetime import datetime, timezone
from app.db.models import Booking, User, Vendor, Service
from tests.test_api import TestingSessionLocal, client, make_auth_headers


@pytest.fixture
def seeded_db():
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    regular = User(
        email=f"regular_{uid}@test.com", username=f"regular_{uid}",
        password="pw", phone="1", f_name="Regular", l_name="User",
        age=25, location="123", gender="M", language="EN", token_version=0,
        is_admin=False,
    )
    admin = User(
        email=f"admin_{uid}@test.com", username=f"admin_{uid}",
        password="pw", phone="1", f_name="Admin", l_name="User",
        age=30, location="456", gender="F", language="EN", token_version=0,
        is_admin=True,
    )
    vendor_user = User(
        email=f"disp_vendor_{uid}@test.com", username=f"disp_vendor_{uid}",
        password="pw", phone="1", f_name="Vendor", l_name="User",
        age=28, location="789", gender="M", language="EN", token_version=0,
    )
    db.add_all([regular, admin, vendor_user])
    db.commit()
    for u in [regular, admin, vendor_user]:
        db.refresh(u)

    vendor = Vendor(user_id=vendor_user.user_id, bio="bio", rating=0.0, num_events=0,
                    stripe_account_id="acct_test", stripe_onboarding_complete=True)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    service = Service(name="Catering", price=1000.0, duration_minutes=300,
                      vendor_id=vendor.vendor_id, experience="10 years")
    db.add(service)
    db.commit()
    db.refresh(service)

    paid_booking = Booking(
        user_id=regular.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id,        time_start="12:00", time_end="18:00", location="Hall",
        date_iso="2026-09-01", status="approved",
        payment_status="paid", payment_intent_id="pi_test_123",
        amount_cents=100000, platform_fee_cents=5000,
        confirmed_at=datetime.now(timezone.utc),
    )
    db.add(paid_booking)
    db.commit()
    db.refresh(paid_booking)

    yield {
        "regular": regular, "admin": admin, "vendor_user": vendor_user,
        "vendor": vendor, "service": service, "paid_booking": paid_booking, "db": db,
    }
    db.close()


# ── Admin endpoints ───────────────────────────────────────────────────

def test_non_admin_cannot_access_admin_endpoints(seeded_db):
    regular = seeded_db["regular"]
    headers = make_auth_headers(regular)

    response = client.get("/admin/users", headers=headers)
    assert response.status_code == 403


def test_admin_can_list_admins(seeded_db):
    admin = seeded_db["admin"]
    headers = make_auth_headers(admin)

    response = client.get("/admin/users", headers=headers)
    assert response.status_code == 200
    emails = [u["email"] for u in response.json()]
    assert admin.email in emails


def test_admin_can_promote_user(seeded_db):
    admin = seeded_db["admin"]
    regular = seeded_db["regular"]
    db = seeded_db["db"]
    headers = make_auth_headers(admin)

    response = client.post(f"/admin/users/{regular.user_id}/make-admin", headers=headers)
    assert response.status_code == 200

    db.refresh(regular)
    assert regular.is_admin is True


def test_admin_can_revoke_admin(seeded_db):
    admin = seeded_db["admin"]
    regular = seeded_db["regular"]
    db = seeded_db["db"]
    headers = make_auth_headers(admin)

    # Promote first
    client.post(f"/admin/users/{regular.user_id}/make-admin", headers=headers)
    db.refresh(regular)
    assert regular.is_admin is True

    # Then revoke
    response = client.post(f"/admin/users/{regular.user_id}/revoke-admin", headers=headers)
    assert response.status_code == 200
    db.refresh(regular)
    assert regular.is_admin is False


def test_admin_cannot_revoke_own_access(seeded_db):
    admin = seeded_db["admin"]
    headers = make_auth_headers(admin)

    response = client.post(f"/admin/users/{admin.user_id}/revoke-admin", headers=headers)
    assert response.status_code == 400


def test_promote_nonexistent_user(seeded_db):
    admin = seeded_db["admin"]
    headers = make_auth_headers(admin)

    response = client.post(f"/admin/users/{uuid.uuid4()}/make-admin", headers=headers)
    assert response.status_code == 404


# ── Disputes ─────────────────────────────────────────────────────────

def test_client_can_raise_dispute(seeded_db):
    regular = seeded_db["regular"]
    paid_booking = seeded_db["paid_booking"]
    db = seeded_db["db"]
    headers = make_auth_headers(regular)

    response = client.post(f"/payments/bookings/{paid_booking.booking_id}/dispute",
                           json={"reason": "Vendor did not show up"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["payment_status"] == "disputed"

    db.refresh(paid_booking)
    assert paid_booking.payment_status == "disputed"


def test_vendor_cannot_raise_dispute(seeded_db):
    vendor_user = seeded_db["vendor_user"]
    paid_booking = seeded_db["paid_booking"]
    headers = make_auth_headers(vendor_user)

    response = client.post(f"/payments/bookings/{paid_booking.booking_id}/dispute",
                           json={}, headers=headers)
    assert response.status_code == 403


def test_cannot_dispute_unpaid_booking(seeded_db):
    db = seeded_db["db"]
    regular = seeded_db["regular"]
    vendor = seeded_db["vendor"]
    service = seeded_db["service"]
    headers = make_auth_headers(regular)

    unpaid = Booking(
        user_id=regular.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id,        time_start="10:00", time_end="12:00", location="Venue",
        date_iso="2026-10-01", status="approved", payment_status="unpaid",
    )
    db.add(unpaid)
    db.commit()
    db.refresh(unpaid)

    response = client.post(f"/payments/bookings/{unpaid.booking_id}/dispute",
                           json={}, headers=headers)
    assert response.status_code == 400


def test_admin_can_resolve_dispute_with_refund(seeded_db, mocker):
    regular = seeded_db["regular"]
    admin = seeded_db["admin"]
    paid_booking = seeded_db["paid_booking"]
    db = seeded_db["db"]

    # Raise the dispute first
    client.post(f"/payments/bookings/{paid_booking.booking_id}/dispute",
                json={}, headers=make_auth_headers(regular))

    mocker.patch("app.services.stripe_service.stripe.Refund.create", return_value={"id": "re_test"})

    response = client.post(
        f"/payments/bookings/{paid_booking.booking_id}/dispute/resolve",
        json={"resolution": "refund_customer"},
        headers=make_auth_headers(admin),
    )
    assert response.status_code == 200
    assert response.json()["payment_status"] == "refunded"

    db.refresh(paid_booking)
    assert paid_booking.payment_status == "refunded"


def test_non_admin_cannot_resolve_dispute(seeded_db):
    regular = seeded_db["regular"]
    paid_booking = seeded_db["paid_booking"]

    client.post(f"/payments/bookings/{paid_booking.booking_id}/dispute",
                json={}, headers=make_auth_headers(regular))

    response = client.post(
        f"/payments/bookings/{paid_booking.booking_id}/dispute/resolve",
        json={"resolution": "refund_customer"},
        headers=make_auth_headers(regular),
    )
    assert response.status_code == 403


def test_invalid_resolution_rejected(seeded_db):
    regular = seeded_db["regular"]
    admin = seeded_db["admin"]
    paid_booking = seeded_db["paid_booking"]

    client.post(f"/payments/bookings/{paid_booking.booking_id}/dispute",
                json={}, headers=make_auth_headers(regular))

    response = client.post(
        f"/payments/bookings/{paid_booking.booking_id}/dispute/resolve",
        json={"resolution": "do_nothing"},
        headers=make_auth_headers(admin),
    )
    assert response.status_code == 400
