"""Stripe Checkout return URLs are client-specific.

iOS returns to this API's /payment-complete, which bounces into the app via the
jorna:// URL scheme — a dead end in a desktop browser. Browser clients pass
`client=web` and must come back into the web app instead.
"""

import uuid
from unittest.mock import patch

import pytest

from app.config import WEB_APP_URL
from app.db.models import Booking, Service, User, Vendor
from tests.test_api import TestingSessionLocal, client, make_auth_headers


@pytest.fixture
def paid_ready_booking():
    """An approved, unpaid booking whose vendor can accept payments."""
    db = TestingSessionLocal()
    uid = uuid.uuid4().hex[:8]

    def _user(prefix):
        u = User(
            email=f"{prefix}_{uid}@test.com", username=f"{prefix}_{uid}",
            password="pw", phone="1", f_name="A", l_name="B",
            age=30, location="NJ", gender="F", language="EN", token_version=0,
        )
        db.add(u); db.commit(); db.refresh(u)
        return u

    customer = _user("co_client")
    vendor_user = _user("co_vendor")

    vendor = Vendor(
        user_id=vendor_user.user_id, bio="b", rating=5.0, num_events=3,
        stripe_account_id="acct_test123", stripe_onboarding_complete=True,
    )
    db.add(vendor); db.commit(); db.refresh(vendor)

    service = Service(name="Checkout Svc", price=500.0, vendor_id=vendor.vendor_id,
                      experience="exp")
    db.add(service); db.commit(); db.refresh(service)

    booking = Booking(
        user_id=customer.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id, time_start="10:00", time_end="12:00",
        location="145 Main St", date_iso="2026-12-01", status="approved",
        payment_status="unpaid", amount_cents=50000,
    )
    db.add(booking); db.commit(); db.refresh(booking)

    yield {"booking": booking, "customer": customer, "db": db}
    db.close()


class _FakeSession:
    id = "cs_test_123"
    url = "https://checkout.stripe.com/c/pay/cs_test_123"


def _checkout(booking, customer, query: str = ""):
    """Call the endpoint with Stripe mocked; return the kwargs Stripe was given."""
    with patch("stripe.checkout.Session.create", return_value=_FakeSession()) as mock:
        resp = client.post(
            f"/payments/bookings/{booking.booking_id}/checkout-session{query}",
            headers=make_auth_headers(customer),
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["checkout_url"] == _FakeSession.url
    return mock.call_args.kwargs


def test_web_client_returns_to_the_web_app(paid_ready_booking):
    kwargs = _checkout(
        paid_ready_booking["booking"], paid_ready_booking["customer"], "?client=web"
    )
    base = WEB_APP_URL.rstrip("/")
    assert kwargs["success_url"].startswith(f"{base}/payment-complete")
    assert kwargs["cancel_url"].startswith(f"{base}/payment-complete")
    assert "status=success" in kwargs["success_url"]
    assert "status=cancel" in kwargs["cancel_url"]


def test_default_client_returns_to_the_api_bridge(paid_ready_booking):
    """Unspecified (i.e. iOS) keeps the existing deep-link bridge behaviour."""
    kwargs = _checkout(paid_ready_booking["booking"], paid_ready_booking["customer"])
    assert "/payment-complete" in kwargs["success_url"]
    assert not kwargs["success_url"].startswith(WEB_APP_URL.rstrip("/"))


class _FakeLink:
    url = "https://connect.stripe.com/setup/s/test"


def _onboard(paid_ready_booking, query: str = ""):
    """Call Stripe Connect onboarding with Stripe mocked; return its kwargs."""
    db = paid_ready_booking["db"]
    vendor = (
        db.query(Vendor)
        .filter(Vendor.vendor_id == paid_ready_booking["booking"].vendor_id)
        .first()
    )
    vendor_user = db.query(User).filter(User.user_id == vendor.user_id).first()
    with patch("stripe.AccountLink.create", return_value=_FakeLink()) as mock:
        resp = client.post(
            f"/payments/vendors/{vendor.vendor_id}/stripe-onboard{query}",
            headers=make_auth_headers(vendor_user),
        )
    assert resp.status_code == 200, resp.text
    return mock.call_args.kwargs


def test_onboarding_web_client_returns_to_the_web_app(paid_ready_booking):
    """Connect onboarding has the same split as checkout — the iOS landing page
    bounces to jorna://, which strands a browser mid-setup."""
    kwargs = _onboard(paid_ready_booking, "?client=web")
    base = WEB_APP_URL.rstrip("/")
    assert kwargs["return_url"].startswith(base)
    assert kwargs["refresh_url"].startswith(base)


def test_onboarding_default_client_uses_the_api_bridge(paid_ready_booking):
    kwargs = _onboard(paid_ready_booking)
    assert not kwargs["return_url"].startswith(WEB_APP_URL.rstrip("/"))


def test_return_url_is_not_client_supplied(paid_ready_booking):
    """An attacker-controlled `client` value can't redirect anywhere else — the
    only web target is the configured WEB_APP_URL."""
    kwargs = _checkout(
        paid_ready_booking["booking"],
        paid_ready_booking["customer"],
        "?client=https://evil.example.com",
    )
    assert "evil.example.com" not in kwargs["success_url"]
    assert "evil.example.com" not in kwargs["cancel_url"]
