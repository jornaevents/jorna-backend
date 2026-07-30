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


# ── A review is about a listing, not just a vendor ─────────────────────────
#
# One vendor selling two things is the case that matters. Before 0040 a review
# reached no further than the vendor, so both listings advertised the same
# blended score: the good one looked worse than it is and the weak one better.


@pytest.fixture
def two_listings():
    """One vendor, two services, three approved bookings — two on the first
    service and one on the second, each by a different client so every booking
    can be reviewed by its own owner."""
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    clients = []
    for i in range(3):
        u = User(
            email=f"two_c{i}_{uid}@test.com", username=f"two_c{i}_{uid}",
            password="pw", phone="1", f_name=f"Client{i}", l_name="User",
            age=25, location="123", gender="M", language="EN", token_version=0,
        )
        db.add(u)
        clients.append(u)
    vendor_user = User(
        email=f"two_v_{uid}@test.com", username=f"two_v_{uid}",
        password="pw", phone="1", f_name="Vendor", l_name="User",
        age=30, location="456", gender="F", language="EN", token_version=0,
    )
    db.add(vendor_user)
    db.commit()
    for u in clients:
        db.refresh(u)
    db.refresh(vendor_user)

    vendor = Vendor(user_id=vendor_user.user_id, bio="bio", rating=0.0, num_events=0)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    mandap = Service(name="Mandap Design", price=3000.0, vendor_id=vendor.vendor_id,
                     experience="10 years")
    floral = Service(name="Floral Styling", price=900.0, vendor_id=vendor.vendor_id,
                     experience="10 years")
    db.add_all([mandap, floral])
    db.commit()
    db.refresh(mandap)
    db.refresh(floral)

    def booking(user, service):
        b = Booking(
            user_id=user.user_id, vendor_id=vendor.vendor_id,
            service_id=service.service_id,
            time_start="10:00", time_end="14:00", location="Hall",
            date_iso="2026-06-01", status="approved",
        )
        db.add(b)
        db.commit()
        db.refresh(b)
        return b

    yield {
        "db": db, "vendor": vendor, "mandap": mandap, "floral": floral,
        "clients": clients,
        "mandap_bookings": [booking(clients[0], mandap), booking(clients[1], mandap)],
        "floral_booking": booking(clients[2], floral),
    }
    db.close()


def _leave(booking, user, rating, comment=None):
    body = {"booking_id": booking.booking_id, "rating": rating}
    if comment is not None:
        body["comment"] = comment
    r = client.post("/reviews", json=body, headers=make_auth_headers(user))
    assert r.status_code == 201, r.text
    return r.json()


def test_a_review_records_which_listing_it_is_about(two_listings):
    """Read off the booking at write time. Without it the review still exists but
    belongs to no page, which is where this started."""
    created = _leave(two_listings["mandap_bookings"][0], two_listings["clients"][0], 5.0)
    assert created["service_id"] == two_listings["mandap"].service_id


def test_two_listings_of_one_vendor_keep_separate_ratings(two_listings):
    """The whole point. Excellent at one thing, ordinary at another — and neither
    number is the average of both."""
    db = two_listings["db"]
    mandap, floral, vendor = two_listings["mandap"], two_listings["floral"], two_listings["vendor"]

    _leave(two_listings["mandap_bookings"][0], two_listings["clients"][0], 5.0, "Beautiful")
    _leave(two_listings["mandap_bookings"][1], two_listings["clients"][1], 4.0, "Lovely")
    _leave(two_listings["floral_booking"], two_listings["clients"][2], 2.0, "Wilted")

    db.refresh(mandap)
    db.refresh(floral)
    db.refresh(vendor)

    assert (mandap.rating, mandap.num_reviews) == (4.5, 2)
    assert (floral.rating, floral.num_reviews) == (2.0, 1)
    # The vendor's own record is unchanged in meaning: every review they have.
    assert vendor.rating == round((5.0 + 4.0 + 2.0) / 3, 2)
    assert vendor.num_events == 3


def test_a_listing_shows_only_its_own_reviews(two_listings):
    _leave(two_listings["mandap_bookings"][0], two_listings["clients"][0], 5.0, "Beautiful")
    _leave(two_listings["mandap_bookings"][1], two_listings["clients"][1], 4.0, "Lovely")
    _leave(two_listings["floral_booking"], two_listings["clients"][2], 2.0, "Wilted")

    r = client.get(f"/reviews/service/{two_listings['mandap'].service_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    assert {i["comment"] for i in data["items"]} == {"Beautiful", "Lovely"}
    assert all(i["service_id"] == two_listings["mandap"].service_id for i in data["items"])


def test_the_vendors_own_page_still_shows_everything(two_listings):
    """Narrowing the service view must not narrow the vendor view — a vendor
    profile is a record of all their work."""
    _leave(two_listings["mandap_bookings"][0], two_listings["clients"][0], 5.0)
    _leave(two_listings["floral_booking"], two_listings["clients"][2], 2.0)

    r = client.get(f"/reviews/vendor/{two_listings['vendor'].vendor_id}")
    assert r.json()["total"] == 2


def test_an_unreviewed_listing_reads_as_new_not_as_bad(two_listings):
    """Zero reviews, and a rating of 0.0 that the page is expected to read
    alongside num_reviews — never as a one-star listing."""
    db = two_listings["db"]
    floral = two_listings["floral"]
    db.refresh(floral)
    assert (floral.rating, floral.num_reviews) == (0.0, 0)

    r = client.get(f"/reviews/service/{floral.service_id}")
    assert r.status_code == 200
    assert r.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}


def test_a_service_carries_its_rating_on_the_api(two_listings):
    """What the service page reads, in the one call it already makes."""
    _leave(two_listings["mandap_bookings"][0], two_listings["clients"][0], 5.0)
    _leave(two_listings["mandap_bookings"][1], two_listings["clients"][1], 4.0)

    r = client.get(f"/services/{two_listings['mandap'].service_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["rating"] == 4.5
    assert body["num_reviews"] == 2


def test_deleting_a_review_lowers_the_listings_rating(two_listings):
    """A rating recomputed from scratch can't drift from the list underneath it."""
    db = two_listings["db"]
    mandap = two_listings["mandap"]

    five = _leave(two_listings["mandap_bookings"][0], two_listings["clients"][0], 5.0)
    _leave(two_listings["mandap_bookings"][1], two_listings["clients"][1], 3.0)
    db.refresh(mandap)
    assert (mandap.rating, mandap.num_reviews) == (4.0, 2)

    r = client.delete(
        f"/reviews/{five['review_id']}",
        headers=make_auth_headers(two_listings["clients"][0]),
    )
    assert r.status_code == 200

    db.refresh(mandap)
    assert (mandap.rating, mandap.num_reviews) == (3.0, 1)


def test_deleting_a_review_leaves_the_sibling_listing_alone(two_listings):
    db = two_listings["db"]
    mandap, floral = two_listings["mandap"], two_listings["floral"]

    doomed = _leave(two_listings["mandap_bookings"][0], two_listings["clients"][0], 5.0)
    _leave(two_listings["floral_booking"], two_listings["clients"][2], 2.0)

    client.delete(
        f"/reviews/{doomed['review_id']}",
        headers=make_auth_headers(two_listings["clients"][0]),
    )

    db.refresh(mandap)
    db.refresh(floral)
    assert (mandap.rating, mandap.num_reviews) == (0.0, 0)
    assert (floral.rating, floral.num_reviews) == (2.0, 1)


def test_deleting_the_last_review_returns_the_listing_to_new(two_listings):
    """Not to one star. 0.0 with a count of 0 is the same state it shipped in."""
    db = two_listings["db"]
    mandap = two_listings["mandap"]

    only = _leave(two_listings["mandap_bookings"][0], two_listings["clients"][0], 1.0)
    client.delete(
        f"/reviews/{only['review_id']}",
        headers=make_auth_headers(two_listings["clients"][0]),
    )
    db.refresh(mandap)
    assert (mandap.rating, mandap.num_reviews) == (0.0, 0)


def test_service_reviews_paginate(two_listings):
    _leave(two_listings["mandap_bookings"][0], two_listings["clients"][0], 5.0, "First")
    _leave(two_listings["mandap_bookings"][1], two_listings["clients"][1], 4.0, "Second")

    sid = two_listings["mandap"].service_id
    page = client.get(f"/reviews/service/{sid}?limit=1&offset=0").json()
    assert page["total"] == 2 and len(page["items"]) == 1

    rest = client.get(f"/reviews/service/{sid}?limit=1&offset=1").json()
    assert len(rest["items"]) == 1
    assert rest["items"][0]["review_id"] != page["items"][0]["review_id"]


def test_reviews_for_an_unknown_service_are_empty_not_an_error(two_listings):
    """A deleted listing's page should read as empty rather than 500."""
    r = client.get(f"/reviews/service/{str(uuid.uuid4())}")
    assert r.status_code == 200
    assert r.json()["total"] == 0
