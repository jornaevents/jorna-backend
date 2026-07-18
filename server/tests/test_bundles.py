"""Tests for the event bundle system."""

import uuid
from datetime import datetime, timezone
import pytest
from app.db.models import (
    Booking, Bundle, User, Vendor, Service, Event,
    Negotiation, NegotiationOffer, Message, Review,
    Conversation, GroupMessage, GroupMessageRead,
)
from tests.test_api import TestingSessionLocal, client, make_auth_headers, engine

from contextlib import contextmanager
from sqlalchemy import event as sa_event


@contextmanager
def count_queries():
    """Count SQL statements executed on the test engine within the block."""
    counter = {"n": 0}

    def _before(conn, cursor, statement, params, context, executemany):
        counter["n"] += 1

    sa_event.listen(engine, "before_cursor_execute", _before)
    try:
        yield counter
    finally:
        sa_event.remove(engine, "before_cursor_execute", _before)


@pytest.fixture
def seeded_db():
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    user = User(
        email=f"bundle_user_{uid}@test.com", username=f"bundle_user_{uid}",
        password="pw", phone="1", f_name="Bundle", l_name="User",
        age=25, location="123", gender="M", language="EN", token_version=0,
    )
    vendor_user = User(
        email=f"bundle_vendor_{uid}@test.com", username=f"bundle_vendor_{uid}",
        password="pw", phone="1", f_name="Vendor", l_name="User",
        age=30, location="456", gender="F", language="EN", token_version=0,
    )
    db.add_all([user, vendor_user])
    db.commit()
    db.refresh(user)
    db.refresh(vendor_user)

    vendor = Vendor(user_id=vendor_user.user_id, bio="bio", rating=4.5, num_events=10)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    service = Service(name="DJ Set", price=1000.0, duration_minutes=240,
                      vendor_id=vendor.vendor_id, experience="5 years")
    db.add(service)
    db.commit()
    db.refresh(service)

    booking1 = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id,
        time_start="18:00", time_end="23:00", location="Hall",
        date_iso="2026-10-01", status="approved",
    )
    booking2 = Booking(
        user_id=user.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id,
        time_start="12:00", time_end="16:00", location="Hall",
        date_iso="2026-10-01", status="pending",
    )
    db.add_all([booking1, booking2])
    db.commit()
    db.refresh(booking1)
    db.refresh(booking2)

    event = Event(
        user_id=user.user_id, name="Raj's Wedding",
        date_iso="2026-10-01", location="Hall",
        event_type="wedding", guest_count=200,
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    yield {
        "user": user, "vendor_user": vendor_user,
        "vendor": vendor, "service": service,
        "booking1": booking1, "booking2": booking2,
        "event": event, "db": db,
    }
    db.close()


def test_create_empty_bundle(seeded_db):
    user = seeded_db["user"]
    response = client.post("/bundles", json={"name": "My Wedding Bundle"},
                           headers=make_auth_headers(user))
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "My Wedding Bundle"
    assert data["status"] == "draft"
    assert data["booking_count"] == 0
    assert data["total_estimated_cost"] == 0.0


def test_create_bundle_with_bookings(seeded_db):
    user = seeded_db["user"]
    booking1 = seeded_db["booking1"]
    booking2 = seeded_db["booking2"]

    response = client.post("/bundles", json={
        "name": "Full Wedding",
        "booking_ids": [booking1.booking_id, booking2.booking_id],
    }, headers=make_auth_headers(user))

    assert response.status_code == 201
    data = response.json()
    assert data["booking_count"] == 2
    assert data["total_estimated_cost"] == 2000.0


def test_create_bundle_with_event(seeded_db):
    user = seeded_db["user"]
    event = seeded_db["event"]

    response = client.post("/bundles", json={
        "name": "Wedding Vendors",
        "event_id": event.event_id,
    }, headers=make_auth_headers(user))

    assert response.status_code == 201
    data = response.json()
    assert data["event_id"] == event.event_id
    assert data["event"]["name"] == "Raj's Wedding"


def test_list_bundles(seeded_db):
    user = seeded_db["user"]
    headers = make_auth_headers(user)

    client.post("/bundles", json={"name": "Bundle A"}, headers=headers)
    client.post("/bundles", json={"name": "Bundle B"}, headers=headers)

    response = client.get("/bundles", headers=headers)
    assert response.status_code == 200
    names = [b["name"] for b in response.json()]
    assert "Bundle A" in names
    assert "Bundle B" in names


def test_list_bundles_query_count_is_constant(seeded_db):
    """N+1 regression: listing bundles must issue a bounded, constant number of
    queries regardless of how many bundles/bookings the user has. Before the
    batch refactor this was 1 + N*2 + N*M*3."""
    from app.services.bundle_service import list_bundles, create_bundle

    user = seeded_db["user"]
    vendor = seeded_db["vendor"]
    service = seeded_db["service"]
    db = seeded_db["db"]

    def make_bundle(n_bookings: int):
        ids = []
        for _ in range(n_bookings):
            bk = Booking(
                user_id=user.user_id, vendor_id=vendor.vendor_id,
                service_id=service.service_id, time_start="10:00",
                time_end="12:00", location="Hall", date_iso="2026-10-01",
                status="pending",
            )
            db.add(bk); db.commit(); db.refresh(bk)
            ids.append(bk.booking_id)
        create_bundle(user_id=user.user_id, name="B", booking_ids=ids, db=db)

    # Small dataset: 2 bundles x 2 bookings
    make_bundle(2)
    make_bundle(2)
    with count_queries() as small:
        small_result = list_bundles(user_id=user.user_id, db=db)

    # 3x the data: 6 bundles x 3 bookings on top
    for _ in range(6):
        make_bundle(3)
    with count_queries() as large:
        large_result = list_bundles(user_id=user.user_id, db=db)

    # Correctness didn't regress: all bundles + their bookings are present.
    assert len(large_result) == 8
    assert sum(b["booking_count"] for b in large_result) == (2 + 2) + 6 * 3

    # The whole point: query count does not grow with data size.
    assert small["n"] == large["n"], (small["n"], large["n"])
    assert large["n"] <= 8  # ~6 batched queries, independent of N and M


def test_list_bundles_pagination(seeded_db):
    """limit/offset page the result; omitting limit returns everything."""
    user = seeded_db["user"]
    headers = make_auth_headers(user)
    for i in range(5):
        client.post("/bundles", json={"name": f"Bundle {i}"}, headers=headers)

    all_bundles = client.get("/bundles", headers=headers).json()
    assert len(all_bundles) == 5

    page = client.get("/bundles?limit=2", headers=headers).json()
    assert len(page) == 2
    # Newest-first ordering is preserved, so paging is stable.
    assert page == all_bundles[:2]

    page2 = client.get("/bundles?limit=2&offset=2", headers=headers).json()
    assert page2 == all_bundles[2:4]


def test_get_bundle(seeded_db):
    user = seeded_db["user"]
    booking1 = seeded_db["booking1"]
    headers = make_auth_headers(user)

    create_resp = client.post("/bundles", json={
        "name": "Test Bundle",
        "booking_ids": [booking1.booking_id],
    }, headers=headers)
    bundle_id = create_resp.json()["bundle_id"]

    response = client.get(f"/bundles/{bundle_id}", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["booking_count"] == 1
    assert "approved" in data["status_breakdown"]


def test_bundle_bookings_expose_escrow_timestamps(seeded_db):
    """Clients need the escrow lifecycle to show release state honestly: who
    still has to confirm, and whether the 24h refund window (which runs from
    paid_at) is open."""
    user = seeded_db["user"]
    booking1 = seeded_db["booking1"]
    headers = make_auth_headers(user)

    create_resp = client.post("/bundles", json={
        "name": "Escrow Fields Bundle",
        "booking_ids": [booking1.booking_id],
    }, headers=headers)
    bundle_id = create_resp.json()["bundle_id"]

    booking = client.get(f"/bundles/{bundle_id}", headers=headers).json()["bookings"][0]
    for field in (
        "paid_at",
        "customer_confirmed_at",
        "vendor_confirmed_at",
        "funds_released_at",
    ):
        assert field in booking, f"{field} missing from the bundle's booking summary"


def test_bundle_bookings_expose_quantity_fields(seeded_db):
    """A swap re-books the same slot with a different service, so the client has
    to carry the quantity across. Without these the replacement would be
    unpayable (price_pending_quantity)."""
    user = seeded_db["user"]
    booking1 = seeded_db["booking1"]
    headers = make_auth_headers(user)

    create_resp = client.post("/bundles", json={
        "name": "Quantity Fields Bundle",
        "booking_ids": [booking1.booking_id],
    }, headers=headers)
    bundle_id = create_resp.json()["bundle_id"]

    booking = client.get(f"/bundles/{bundle_id}", headers=headers).json()["bookings"][0]
    for field in ("guest_count", "date_end"):
        assert field in booking, f"{field} missing from the bundle's booking summary"


def test_add_booking_to_bundle(seeded_db):
    user = seeded_db["user"]
    booking1 = seeded_db["booking1"]
    booking2 = seeded_db["booking2"]
    headers = make_auth_headers(user)

    create_resp = client.post("/bundles", json={
        "name": "Test", "booking_ids": [booking1.booking_id],
    }, headers=headers)
    bundle_id = create_resp.json()["bundle_id"]

    response = client.post(f"/bundles/{bundle_id}/bookings/{booking2.booking_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["booking_count"] == 2


def test_remove_booking_from_bundle(seeded_db):
    user = seeded_db["user"]
    booking1 = seeded_db["booking1"]
    booking2 = seeded_db["booking2"]
    headers = make_auth_headers(user)

    create_resp = client.post("/bundles", json={
        "name": "Test",
        "booking_ids": [booking1.booking_id, booking2.booking_id],
    }, headers=headers)
    bundle_id = create_resp.json()["bundle_id"]

    response = client.delete(f"/bundles/{bundle_id}/bookings/{booking1.booking_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["booking_count"] == 1


def test_booking_cannot_be_in_two_bundles(seeded_db):
    user = seeded_db["user"]
    booking1 = seeded_db["booking1"]
    headers = make_auth_headers(user)

    client.post("/bundles", json={"name": "Bundle A", "booking_ids": [booking1.booking_id]}, headers=headers)
    resp_b = client.post("/bundles", json={"name": "Bundle B"}, headers=headers)
    bundle_b_id = resp_b.json()["bundle_id"]

    response = client.post(f"/bundles/{bundle_b_id}/bookings/{booking1.booking_id}", headers=headers)
    assert response.status_code == 400


def test_update_bundle_status(seeded_db):
    user = seeded_db["user"]
    headers = make_auth_headers(user)

    create_resp = client.post("/bundles", json={"name": "Status Test"}, headers=headers)
    bundle_id = create_resp.json()["bundle_id"]

    response = client.patch(f"/bundles/{bundle_id}/status", json={"status": "confirmed"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"


def test_invalid_status_rejected(seeded_db):
    user = seeded_db["user"]
    headers = make_auth_headers(user)

    create_resp = client.post("/bundles", json={"name": "Status Test"}, headers=headers)
    bundle_id = create_resp.json()["bundle_id"]

    response = client.patch(f"/bundles/{bundle_id}/status", json={"status": "invalid"}, headers=headers)
    assert response.status_code == 400


def test_delete_bundle_deletes_bookings(seeded_db):
    user = seeded_db["user"]
    booking1 = seeded_db["booking1"]
    db = seeded_db["db"]
    headers = make_auth_headers(user)

    create_resp = client.post("/bundles", json={
        "name": "To Delete", "booking_ids": [booking1.booking_id],
    }, headers=headers)
    bundle_id = create_resp.json()["bundle_id"]

    response = client.delete(f"/bundles/{bundle_id}", headers=headers)
    assert response.status_code == 204

    assert db.query(Booking).filter(Booking.booking_id == booking1.booking_id).first() is None
    assert db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first() is None


def test_delete_bundle_with_negotiations_and_chats(seeded_db):
    """Deleting a confirmed bundle should also clean up negotiations, messages,
    reviews, and group conversations tied to its bookings."""
    user = seeded_db["user"]
    vendor_user = seeded_db["vendor_user"]
    vendor = seeded_db["vendor"]
    booking1 = seeded_db["booking1"]
    booking1_id = booking1.booking_id
    db = seeded_db["db"]
    headers = make_auth_headers(user)

    create_resp = client.post("/bundles", json={
        "name": "Full Bundle", "booking_ids": [booking1_id],
    }, headers=headers)
    bundle_id = create_resp.json()["bundle_id"]

    confirm_resp = client.patch(f"/bundles/{bundle_id}/status", json={"status": "confirmed"}, headers=headers)
    assert confirm_resp.status_code == 200

    now = datetime.now(timezone.utc)

    negotiation = Negotiation(
        booking_id=booking1_id, status="open",
        current_offer_cents=10000, proposed_by=user.user_id,
        created_at=now, updated_at=now,
    )
    db.add(negotiation)
    db.commit()
    negotiation_id = negotiation.negotiation_id

    db.add(NegotiationOffer(
        negotiation_id=negotiation_id, proposed_by=user.user_id,
        action="offer", amount_cents=10000, message="test offer", created_at=now,
    ))
    db.add(Message(
        booking_id=booking1_id, sender_id=user.user_id, receiver_id=vendor_user.user_id,
        content="hello", created_at=now, is_read=False,
    ))
    db.add(Review(
        booking_id=booking1_id, vendor_id=vendor.vendor_id, user_id=user.user_id,
        rating=5.0, comment="great", created_at=now,
    ))
    db.commit()

    conversations = db.query(Conversation).filter(Conversation.bundle_id == bundle_id).all()
    assert len(conversations) > 0
    conv_id = conversations[0].conversation_id

    group_msg = GroupMessage(
        conversation_id=conv_id, sender_id=user.user_id,
        content="hi everyone", created_at=now,
    )
    db.add(group_msg)
    db.commit()
    group_msg_id = group_msg.message_id

    db.add(GroupMessageRead(message_id=group_msg_id, user_id=user.user_id, read_at=now))
    db.commit()

    response = client.delete(f"/bundles/{bundle_id}", headers=headers)
    assert response.status_code == 204

    assert db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first() is None
    assert db.query(Booking).filter(Booking.booking_id == booking1_id).first() is None
    assert db.query(Negotiation).filter(Negotiation.booking_id == booking1_id).first() is None
    assert db.query(Conversation).filter(Conversation.bundle_id == bundle_id).first() is None
    assert db.query(GroupMessage).filter(GroupMessage.conversation_id == conv_id).first() is None


def test_bundle_auto_links_event_and_cascade_deletes(seeded_db):
    """A bundle with bookings but no event gets one auto-created (surfaced via the
    serializer), and deleting the bundle removes that auto-created event."""
    from app.services import bundle_service
    user = seeded_db["user"]
    booking1 = seeded_db["booking1"]
    db = seeded_db["db"]
    now = datetime.now(timezone.utc)

    bundle = Bundle(
        user_id=user.user_id, name="AI Bundle", event_name="Sangeet Night",
        status="draft", created_at=now, updated_at=now,
    )
    db.add(bundle)
    db.flush()
    booking1.bundle_id = bundle.bundle_id
    db.commit()
    bundle_id = bundle.bundle_id

    assert bundle.event_id is None
    bundle_service._ensure_bundle_event(bundle, db)
    db.commit()

    event_id = bundle.event_id
    assert event_id is not None
    event = db.query(Event).filter(Event.event_id == event_id).first()
    assert event is not None
    assert event.name == "Sangeet Night"
    assert event.user_id == user.user_id

    headers = make_auth_headers(user)
    resp = client.get(f"/bundles/{bundle_id}", headers=headers)
    assert resp.json()["event_id"] == event_id

    del_resp = client.delete(f"/bundles/{bundle_id}", headers=headers)
    assert del_resp.status_code == 204
    assert db.query(Event).filter(Event.event_id == event_id).first() is None


def test_delete_bundle_keeps_event_shared_by_another_bundle(seeded_db):
    """When two bundles reference the same event, deleting one keeps the event."""
    user = seeded_db["user"]
    event = seeded_db["event"]
    db = seeded_db["db"]
    now = datetime.now(timezone.utc)

    b1 = Bundle(user_id=user.user_id, name="B1", event_id=event.event_id,
                status="draft", created_at=now, updated_at=now)
    b2 = Bundle(user_id=user.user_id, name="B2", event_id=event.event_id,
                status="draft", created_at=now, updated_at=now)
    db.add_all([b1, b2])
    db.commit()
    b1_id = b1.bundle_id

    resp = client.delete(f"/bundles/{b1_id}", headers=make_auth_headers(user))
    assert resp.status_code == 204
    assert db.query(Event).filter(Event.event_id == event.event_id).first() is not None


class TestLegacyDataCleanup:
    """cleanup_legacy_bundle_event_data — orphan AI events, stale comparison
    drafts, duplicate bookings; dry-run must not modify anything."""

    def _cleanup(self, db, **kwargs):
        from app.services import bundle_service
        return bundle_service.cleanup_legacy_bundle_event_data(db=db, **kwargs)

    def test_orphan_ai_event_deleted_user_event_kept(self, seeded_db):
        db = seeded_db["db"]
        user = seeded_db["user"]
        user_event = seeded_db["event"]  # user-created, no marker

        orphan = Event(
            user_id=user.user_id, name="AI Orphan",
            date_iso="2026-11-01", location="Hall",
            description="Bundle from jornAI",
        )
        linked = Event(
            user_id=user.user_id, name="AI Linked",
            date_iso="2026-11-02", location="Hall",
            description="Bundle from jornAI",
        )
        db.add_all([orphan, linked])
        db.commit()
        now = datetime.now(timezone.utc)
        db.add(Bundle(user_id=user.user_id, event_id=linked.event_id, name="Linked",
                      status="draft", created_at=now, updated_at=now))
        db.commit()
        orphan_id, linked_id, user_event_id = orphan.event_id, linked.event_id, user_event.event_id

        # Dry run reports the orphan but deletes nothing
        report = self._cleanup(db, dry_run=True)
        assert orphan_id in [e["event_id"] for e in report["orphan_ai_events"]]
        assert db.query(Event).filter(Event.event_id == orphan_id).first() is not None

        # Real run deletes only the unreferenced AI event
        report = self._cleanup(db, dry_run=False)
        assert db.query(Event).filter(Event.event_id == orphan_id).first() is None
        assert db.query(Event).filter(Event.event_id == linked_id).first() is not None
        assert db.query(Event).filter(Event.event_id == user_event_id).first() is not None

    def test_duplicate_bookings_deduped_keeping_negotiated(self, seeded_db):
        db = seeded_db["db"]
        user = seeded_db["user"]
        vendor = seeded_db["vendor"]
        service = seeded_db["service"]
        now = datetime.now(timezone.utc)

        bundle = Bundle(user_id=user.user_id, name="Dupes", status="confirmed",
                        created_at=now, updated_at=now)
        db.add(bundle)
        db.flush()
        # Same vendor+service+date twice — the select+confirm double-create bug.
        dup_a = Booking(user_id=user.user_id, vendor_id=vendor.vendor_id,
                        service_id=service.service_id, time_start="18:00", time_end="23:00",
                        location="TBD", date_iso="2026-12-01", status="pending",
                        bundle_id=bundle.bundle_id)
        dup_b = Booking(user_id=user.user_id, vendor_id=vendor.vendor_id,
                        service_id=service.service_id, time_start="18:00", time_end="23:00",
                        location="Edison Hall", date_iso="2026-12-01", status="pending",
                        bundle_id=bundle.bundle_id)
        db.add_all([dup_a, dup_b])
        db.commit()
        # The one with a negotiation must win even though the other has a location.
        db.add(Negotiation(booking_id=dup_a.booking_id, status="open",
                           current_offer_cents=90000, proposed_by=user.user_id,
                           created_at=now, updated_at=now))
        db.commit()
        a_id, b_id = dup_a.booking_id, dup_b.booking_id

        report = self._cleanup(db, dry_run=False)
        removed = [r["booking_id"] for r in report["duplicate_bookings_removed"]]
        assert b_id in removed
        assert db.query(Booking).filter(Booking.booking_id == a_id).first() is not None
        assert db.query(Booking).filter(Booking.booking_id == b_id).first() is None

    def test_paid_duplicate_never_deleted(self, seeded_db):
        db = seeded_db["db"]
        user = seeded_db["user"]
        vendor = seeded_db["vendor"]
        service = seeded_db["service"]
        now = datetime.now(timezone.utc)

        bundle = Bundle(user_id=user.user_id, name="PaidDupes", status="confirmed",
                        created_at=now, updated_at=now)
        db.add(bundle)
        db.flush()
        paid = Booking(user_id=user.user_id, vendor_id=vendor.vendor_id,
                       service_id=service.service_id, time_start="10:00", time_end="14:00",
                       location="TBD", date_iso="2026-12-05", status="approved",
                       payment_status="paid", bundle_id=bundle.bundle_id)
        unpaid = Booking(user_id=user.user_id, vendor_id=vendor.vendor_id,
                         service_id=service.service_id, time_start="10:00", time_end="14:00",
                         location="Edison Hall", date_iso="2026-12-05", status="pending",
                         bundle_id=bundle.bundle_id)
        db.add_all([paid, unpaid])
        db.commit()
        paid_id, unpaid_id = paid.booking_id, unpaid.booking_id

        self._cleanup(db, dry_run=False)
        assert db.query(Booking).filter(Booking.booking_id == paid_id).first() is not None
        assert db.query(Booking).filter(Booking.booking_id == unpaid_id).first() is None

    def test_stale_comparison_draft_removed_recent_kept(self, seeded_db):
        from datetime import timedelta
        db = seeded_db["db"]
        user = seeded_db["user"]
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        now = datetime.now(timezone.utc)

        stale = Bundle(user_id=user.user_id, name="Old comparison", status="draft",
                       bundle_group_id="group-1", created_at=old, updated_at=old)
        fresh = Bundle(user_id=user.user_id, name="Fresh comparison", status="draft",
                       bundle_group_id="group-2", created_at=now, updated_at=now)
        chosen = Bundle(user_id=user.user_id, name="Chosen", status="draft",
                        bundle_group_id=None, created_at=old, updated_at=old)
        db.add_all([stale, fresh, chosen])
        db.commit()
        stale_id, fresh_id, chosen_id = stale.bundle_id, fresh.bundle_id, chosen.bundle_id

        report = self._cleanup(db, dry_run=False, stale_days=7)
        assert stale_id in [b["bundle_id"] for b in report["stale_draft_bundles"]]
        assert db.query(Bundle).filter(Bundle.bundle_id == stale_id).first() is None
        assert db.query(Bundle).filter(Bundle.bundle_id == fresh_id).first() is not None
        # No group id => a normal draft the user is still working on. Kept.
        assert db.query(Bundle).filter(Bundle.bundle_id == chosen_id).first() is not None

    def test_orphan_conversations_hidden_and_purged(self, seeded_db):
        """Chats whose bundle is gone are excluded from listings and removed
        by the cleanup."""
        from datetime import datetime, timezone
        from app.db.models import Conversation, ConversationMember, GroupMessage
        from app.services.conversation_service import list_conversations

        db = seeded_db["db"]
        user = seeded_db["user"]
        now = datetime.now(timezone.utc)

        # A live bundle with a conversation, and an orphan conversation whose
        # bundle no longer exists.
        live = Bundle(user_id=user.user_id, name="Live", status="confirmed",
                      created_at=now, updated_at=now)
        db.add(live)
        db.flush()
        conv_live = Conversation(bundle_id=live.bundle_id, type="all_parties",
                                 name="Live chat", created_at=now)
        conv_orphan = Conversation(bundle_id="bundle-gone-123", type="all_parties",
                                   name="Ghost chat", created_at=now)
        db.add_all([conv_live, conv_orphan])
        db.flush()
        db.add_all([
            ConversationMember(conversation_id=conv_live.conversation_id, user_id=user.user_id, joined_at=now),
            ConversationMember(conversation_id=conv_orphan.conversation_id, user_id=user.user_id, joined_at=now),
            GroupMessage(conversation_id=conv_orphan.conversation_id, sender_id=user.user_id,
                         content="lingering", created_at=now),
        ])
        db.commit()
        orphan_id = conv_orphan.conversation_id

        # Listing hides the orphan but keeps the live chat.
        listed = list_conversations(caller_user_id=user.user_id, db=db)
        listed_ids = {c["conversation_id"] for c in listed}
        assert conv_live.conversation_id in listed_ids
        assert orphan_id not in listed_ids

        # Cleanup reports it on dry run and deletes it for real.
        report = self._cleanup(db, dry_run=True)
        assert orphan_id in [c["conversation_id"] for c in report["orphan_conversations"]]
        assert db.query(Conversation).filter(Conversation.conversation_id == orphan_id).first() is not None

        self._cleanup(db, dry_run=False)
        assert db.query(Conversation).filter(Conversation.conversation_id == orphan_id).first() is None
        assert db.query(GroupMessage).filter(GroupMessage.conversation_id == orphan_id).first() is None
        assert db.query(Conversation).filter(Conversation.conversation_id == conv_live.conversation_id).first() is not None

    def test_admin_endpoint_requires_admin(self, seeded_db):
        user = seeded_db["user"]
        db = seeded_db["db"]

        resp = client.post("/admin/cleanup/bundle-event-data", headers=make_auth_headers(user))
        assert resp.status_code == 403

        user.is_admin = True
        db.commit()
        resp = client.post("/admin/cleanup/bundle-event-data", headers=make_auth_headers(user))
        assert resp.status_code == 200
        assert resp.json()["dry_run"] is True


def test_other_user_cannot_access_bundle(seeded_db):
    user = seeded_db["user"]
    vendor_user = seeded_db["vendor_user"]

    create_resp = client.post("/bundles", json={"name": "Private"},
                              headers=make_auth_headers(user))
    bundle_id = create_resp.json()["bundle_id"]

    response = client.get(f"/bundles/{bundle_id}", headers=make_auth_headers(vendor_user))
    assert response.status_code == 403
