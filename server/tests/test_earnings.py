"""Tests for the vendor earnings endpoint."""

import uuid
from datetime import datetime, timezone
import pytest
from app.db.models import Booking, Bundle, User, Vendor, Service
from tests.test_api import TestingSessionLocal, client, make_auth_headers


@pytest.fixture
def earnings_db():
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    client_user = User(
        email=f"earn_client_{uid}@test.com", username=f"earn_client_{uid}", password="pw",
        phone="1", f_name="Cleo", l_name="Client", age=25, location="NJ",
        gender="F", language="EN", token_version=0,
    )
    vendor_user = User(
        email=f"earn_vendor_{uid}@test.com", username=f"earn_vendor_{uid}", password="pw",
        phone="1", f_name="Vik", l_name="Vendor", age=30, location="NJ",
        gender="M", language="EN", token_version=0,
    )
    db.add_all([client_user, vendor_user])
    db.commit()
    db.refresh(client_user)
    db.refresh(vendor_user)

    vendor = Vendor(user_id=vendor_user.user_id, bio="bio", rating=4.8, num_events=12)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    service = Service(name="DJ Set", price=1000.0, duration_minutes=240,
                      vendor_id=vendor.vendor_id, experience="8 years")
    db.add(service)
    db.commit()
    db.refresh(service)

    now = datetime.now(timezone.utc)

    def booking(status, payment_status, amount=None, fee=None, paid=False, released=False):
        return Booking(
            user_id=client_user.user_id, vendor_id=vendor.vendor_id,
            service_id=service.service_id, time_start="18:00", time_end="23:00",
            location="Hall", date_iso="2026-10-01", status=status,
            payment_status=payment_status, amount_cents=amount, platform_fee_cents=fee,
            paid_at=now if paid else None, funds_released_at=now if released else None,
        )

    released1 = booking("completed", "released", amount=100_000, fee=5_000, paid=True, released=True)
    escrow1 = booking("approved", "paid", amount=50_000, fee=2_500, paid=True)
    upcoming1 = booking("approved", "unpaid")            # falls back to service price
    pending1 = booking("pending", "unpaid")              # not approved — excluded from upcoming
    db.add_all([released1, escrow1, upcoming1, pending1])
    db.commit()

    yield {
        "client_user": client_user, "vendor_user": vendor_user,
        "vendor": vendor, "db": db,
    }
    db.close()


def test_earnings_math(earnings_db):
    vendor = earnings_db["vendor"]
    vendor_user = earnings_db["vendor_user"]

    resp = client.get(
        f"/payments/vendors/{vendor.vendor_id}/earnings",
        headers=make_auth_headers(vendor_user),
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_released_cents"] == 95_000       # 100k − 5k fee
    assert data["in_escrow_cents"] == 47_500            # 50k − 2.5k fee
    assert data["upcoming_cents"] == 100_000            # service price fallback ($1000)
    assert data["upcoming_count"] == 1
    assert data["platform_fees_cents"] == 5_000

    # History includes only payment-active bookings (released + escrow)
    assert len(data["history"]) == 2
    statuses = {e["payment_status"] for e in data["history"]}
    assert statuses == {"released", "paid"}
    assert all(e["client_name"] == "Cleo Client" for e in data["history"])


def test_earnings_self_reported_income(earnings_db):
    """Manual-track income (self-reported, never touched by Jorna) is its
    own bucket — never blended into total_released_cents/in_escrow_cents."""
    db = earnings_db["db"]
    vendor = earnings_db["vendor"]
    vendor_user = earnings_db["vendor_user"]
    client_user = earnings_db["client_user"]
    service = db.query(Service).filter(Service.vendor_id == vendor.vendor_id).first()

    confirmed = Booking(
        user_id=client_user.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id, time_start="18:00", time_end="23:00",
        location="Hall", date_iso="2026-10-02", status="approved",
        payment_status="confirmed_paid", payment_method="manual", amount_cents=80_000,
    )
    awaiting = Booking(
        user_id=client_user.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id, time_start="18:00", time_end="23:00",
        location="Hall", date_iso="2026-10-03", status="approved",
        payment_status="marked_paid", payment_method="manual", amount_cents=30_000,
    )
    db.add_all([confirmed, awaiting])
    db.commit()

    resp = client.get(
        f"/payments/vendors/{vendor.vendor_id}/earnings",
        headers=make_auth_headers(vendor_user),
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["self_reported_cents"] == 80_000
    assert data["self_reported_pending_cents"] == 30_000
    assert data["self_reported_pending_count"] == 1
    # Doesn't leak into the Stripe-verified buckets from the base fixture.
    assert data["total_released_cents"] == 95_000
    assert data["in_escrow_cents"] == 47_500


def test_earnings_requires_owner(earnings_db):
    vendor = earnings_db["vendor"]
    client_user = earnings_db["client_user"]

    resp = client.get(
        f"/payments/vendors/{vendor.vendor_id}/earnings",
        headers=make_auth_headers(client_user),
    )
    assert resp.status_code == 403

    resp = client.get(f"/payments/vendors/{vendor.vendor_id}/earnings")
    assert resp.status_code in (401, 403)
