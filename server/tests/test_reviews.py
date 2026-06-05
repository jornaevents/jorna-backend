"""Tests for the reviews and ratings system."""

import uuid
import pytest
from app.db.models import Booking, User, Vendor, Service, Review
from tests.test_api import TestingSessionLocal, client, make_auth_headers


@pytest.fixture
def seeded_db():
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    client_user = User(
        email=f"review_client_{uid}@test.com", username=f"review_client_{uid}",
        password="pw", phone="1", f_name="Client", l_name="User",
        age=25, location="123", gender="M", language="EN", token_version=0,
    )
    vendor_user = User(
        email=f"review_vendor_{uid}@test.com", username=f"review_vendor_{uid}",
        password="pw", phone="1", f_name="Vendor", l_name="User",
        age=30, location="456", gender="F", language="EN", token_version=0,
    )
    db.add_all([client_user, vendor_user])
    db.commit()
    db.refresh(client_user)
    db.refresh(vendor_user)

    vendor = Vendor(user_id=vendor_user.user_id, bio="bio", rating=0.0, num_events=0)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    service = Service(name="Photography", price=500.0, duration_minutes=120,
                      vendor_id=vendor.vendor_id, experience="5 years")
    db.add(service)
    db.commit()
    db.refresh(service)

    booking = Booking(
        user_id=client_user.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id,
        time_start="10:00", time_end="14:00", location="Hall",
        date_iso="2026-06-01", status="approved",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    yield {
        "client_user": client_user, "vendor_user": vendor_user,
        "vendor": vendor, "service": service, "booking": booking, "db": db,
    }
    db.close()


def test_create_review(seeded_db):
    client_user = seeded_db["client_user"]
    booking = seeded_db["booking"]
    headers = make_auth_headers(client_user)

    response = client.post("/reviews", json={
        "booking_id": booking.booking_id,
        "rating": 5.0,
        "comment": "Excellent service!",
    }, headers=headers)

    assert response.status_code == 201
    data = response.json()
    assert data["rating"] == 5.0
    assert data["comment"] == "Excellent service!"
    assert data["user_id"] == client_user.user_id


def test_review_updates_vendor_rating(seeded_db):
    db = seeded_db["db"]
    vendor = seeded_db["vendor"]
    client_user = seeded_db["client_user"]
    booking = seeded_db["booking"]
    headers = make_auth_headers(client_user)

    client.post("/reviews", json={
        "booking_id": booking.booking_id,
        "rating": 4.0,
        "comment": "Good!",
    }, headers=headers)

    db.refresh(vendor)
    assert vendor.rating == 4.0
    assert vendor.num_events == 1


def test_duplicate_review_rejected(seeded_db):
    client_user = seeded_db["client_user"]
    booking = seeded_db["booking"]
    headers = make_auth_headers(client_user)

    client.post("/reviews", json={"booking_id": booking.booking_id, "rating": 5.0}, headers=headers)
    response = client.post("/reviews", json={"booking_id": booking.booking_id, "rating": 3.0}, headers=headers)
    assert response.status_code == 400
    assert "already reviewed" in response.json()["detail"]


def test_vendor_cannot_review_own_booking(seeded_db):
    vendor_user = seeded_db["vendor_user"]
    booking = seeded_db["booking"]
    headers = make_auth_headers(vendor_user)

    response = client.post("/reviews", json={"booking_id": booking.booking_id, "rating": 5.0}, headers=headers)
    assert response.status_code == 403


def test_invalid_rating_rejected(seeded_db):
    client_user = seeded_db["client_user"]
    booking = seeded_db["booking"]
    headers = make_auth_headers(client_user)

    response = client.post("/reviews", json={"booking_id": booking.booking_id, "rating": 6.0}, headers=headers)
    assert response.status_code == 400


def test_pending_booking_cannot_be_reviewed(seeded_db):
    db = seeded_db["db"]
    client_user = seeded_db["client_user"]
    vendor = seeded_db["vendor"]
    service = seeded_db["service"]
    headers = make_auth_headers(client_user)

    pending_booking = Booking(
        user_id=client_user.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id,
        time_start="10:00", time_end="12:00", location="Hall",
        date_iso="2026-07-01", status="pending",
    )
    db.add(pending_booking)
    db.commit()
    db.refresh(pending_booking)

    response = client.post("/reviews", json={"booking_id": pending_booking.booking_id, "rating": 5.0}, headers=headers)
    assert response.status_code == 400


def test_get_vendor_reviews(seeded_db):
    db = seeded_db["db"]
    client_user = seeded_db["client_user"]
    vendor = seeded_db["vendor"]
    booking = seeded_db["booking"]
    headers = make_auth_headers(client_user)

    client.post("/reviews", json={"booking_id": booking.booking_id, "rating": 4.5, "comment": "Great!"}, headers=headers)

    response = client.get(f"/reviews/vendor/{vendor.vendor_id}")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "total" in data
    assert len(data["items"]) >= 1
    assert data["items"][0]["rating"] == 4.5


def test_get_booking_review(seeded_db):
    db = seeded_db["db"]
    client_user = seeded_db["client_user"]
    booking = seeded_db["booking"]
    headers = make_auth_headers(client_user)

    client.post("/reviews", json={"booking_id": booking.booking_id, "rating": 5.0}, headers=headers)

    response = client.get(f"/reviews/booking/{booking.booking_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["rating"] == 5.0


def test_get_booking_review_not_found(seeded_db):
    client_user = seeded_db["client_user"]
    headers = make_auth_headers(client_user)

    response = client.get(f"/reviews/booking/{str(uuid.uuid4())}", headers=headers)
    assert response.status_code == 404
