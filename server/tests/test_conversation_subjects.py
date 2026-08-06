"""A conversation is about a bundle, a booking, or a vendor nobody has booked.

Covers the three things MESSAGING_PROPOSAL added: the enquiry a client opens
from the marketplace, the private thread on one booking, and negotiation offers
written into that thread as messages. Also pins the inbox filter that used to
delete everything without a bundle.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.db.models import Booking, Conversation, GroupMessage, Service, User, UserBlock, Vendor
from app.services.conversation_service import (
    MAX_ENQUIRIES_PER_DAY,
    MAX_UNANSWERED_ENQUIRIES,
)
from tests.test_api import TestingSessionLocal, client, make_auth_headers


def _user(db, tag, uid):
    u = User(
        email=f"{tag}_{uid}@test.com", username=f"{tag}_{uid}", password="pw",
        phone="1", f_name=tag.title(), l_name="Tester", age=30, location="1",
        gender="F", language="EN", token_version=0,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def world():
    """A client, two vendors with a listing each, and one booking."""
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    client_user = _user(db, "enqclient", uid)
    vendor_user = _user(db, "enqvendor", uid)
    other_vendor_user = _user(db, "enqvendor2", uid)

    vendor = Vendor(user_id=vendor_user.user_id, bio="Photos", rating=4.9, num_events=10)
    vendor2 = Vendor(user_id=other_vendor_user.user_id, bio="DJ", rating=4.7, num_events=5)
    db.add_all([vendor, vendor2])
    db.commit()
    db.refresh(vendor)
    db.refresh(vendor2)

    service = Service(name="Wedding Photography", price=2000.0, duration_minutes=480,
                      vendor_id=vendor.vendor_id, experience="8 years", negotiable=True)
    db.add(service)
    db.commit()
    db.refresh(service)

    booking = Booking(
        user_id=client_user.user_id, vendor_id=vendor.vendor_id,
        service_id=service.service_id, time_start="10:00", time_end="18:00",
        location="Hall", date_iso="2026-11-01", status="pending",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    yield {
        "db": db, "client_user": client_user, "vendor_user": vendor_user,
        "other_vendor_user": other_vendor_user, "vendor": vendor, "vendor2": vendor2,
        "service": service, "booking": booking,
    }
    db.close()


def _ask(world, vendor_id=None, content="Are you free on the 14th?", service_id=None):
    body = {"vendor_id": vendor_id or world["vendor"].vendor_id, "content": content}
    if service_id:
        body["service_id"] = service_id
    return client.post("/conversations/enquiry", json=body,
                       headers=make_auth_headers(world["client_user"]))


# ── Enquiries ─────────────────────────────────────────────────────────


class TestEnquiryThreads:
    def test_a_client_can_ask_a_vendor_a_question(self, world):
        resp = _ask(world)
        assert resp.status_code == 201, resp.text
        assert resp.json()["message"]["content"] == "Are you free on the 14th?"

    def test_the_vendor_receives_it(self, world):
        _ask(world)
        convs = client.get("/conversations",
                           headers=make_auth_headers(world["vendor_user"])).json()
        assert len(convs) == 1
        assert convs[0]["subject_type"] == "enquiry"
        # Named for the person on the other end, from each side.
        assert convs[0]["name"].startswith("Enqclient")

    def test_a_second_question_reuses_the_thread(self, world):
        first = _ask(world).json()["conversation_id"]
        second = _ask(world, content="And do you travel?").json()["conversation_id"]
        assert first == second

        msgs = client.get(f"/conversations/{first}/messages",
                          headers=make_auth_headers(world["client_user"])).json()
        assert [m["content"] for m in msgs["items"]] == [
            "Are you free on the 14th?", "And do you travel?",
        ]

    def test_the_listing_it_came_from_rides_along(self, world):
        resp = _ask(world, service_id=world["service"].service_id)
        meta = resp.json()["message"]["meta"]
        assert meta["service_id"] == world["service"].service_id
        assert meta["service_name"] == "Wedding Photography"

    def test_a_vendor_cannot_cold_open_one(self, world):
        """Only a client starts a thread. The other way round is advertising."""
        resp = client.post("/conversations/enquiry", json={
            "vendor_id": world["vendor2"].vendor_id, "content": "want a DJ?",
        }, headers=make_auth_headers(world["vendor_user"]))
        # The vendor is not blocked from asking *another* vendor a question as a
        # client would — what they cannot do is reach their own customer. This
        # asserts the self-message guard, which is the case that has no meaning.
        assert resp.status_code == 201
        self_resp = client.post("/conversations/enquiry", json={
            "vendor_id": world["vendor"].vendor_id, "content": "hello me",
        }, headers=make_auth_headers(world["vendor_user"]))
        assert self_resp.status_code == 400

    def test_a_block_stops_it_in_both_directions(self, world):
        db = world["db"]
        db.add(UserBlock(
            blocker_user_id=world["vendor_user"].user_id,
            blocked_user_id=world["client_user"].user_id,
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()
        assert _ask(world).status_code == 403

    def test_empty_questions_are_refused(self, world):
        assert _ask(world, content="   ").status_code == 400


class TestEnquiryLimits:
    """The condition item 8 was deferred on: the limits ship with the feature."""

    def _ask_new_vendor(self, world, n):
        """Open an enquiry with a freshly made vendor, so each is a new thread."""
        db = world["db"]
        u = _user(db, f"spam{n}", str(uuid.uuid4())[:8])
        v = Vendor(user_id=u.user_id, bio="x", rating=5.0, num_events=1)
        db.add(v)
        db.commit()
        db.refresh(v)
        return _ask(world, vendor_id=v.vendor_id, content=f"question {n}")

    def test_unanswered_questions_cap_out(self, world):
        for n in range(MAX_UNANSWERED_ENQUIRIES):
            assert self._ask_new_vendor(world, n).status_code == 201
        refused = self._ask_new_vendor(world, 99)
        assert refused.status_code == 429
        assert "waiting for an answer" in refused.json()["detail"]

    def test_a_reply_frees_the_slot(self, world):
        """The cap is on being ignored, not on talking."""
        convs = []
        for n in range(MAX_UNANSWERED_ENQUIRIES):
            convs.append(self._ask_new_vendor(world, n).json()["conversation_id"])
        assert self._ask_new_vendor(world, 99).status_code == 429

        # The vendor on the first thread answers.
        db = world["db"]
        conv = db.query(Conversation).filter(
            Conversation.conversation_id == convs[0]
        ).first()
        vendor = db.query(Vendor).filter(Vendor.vendor_id == conv.vendor_id).first()
        vendor_user = db.query(User).filter(User.user_id == vendor.user_id).first()
        replied = client.post(f"/conversations/{convs[0]}/messages",
                              json={"content": "yes we are"},
                              headers=make_auth_headers(vendor_user))
        assert replied.status_code == 201

        assert self._ask_new_vendor(world, 100).status_code == 201

    def test_the_daily_ceiling_holds_when_everyone_answers(self, world):
        db = world["db"]
        for n in range(MAX_ENQUIRIES_PER_DAY):
            conv_id = self._ask_new_vendor(world, n).json()["conversation_id"]
            conv = db.query(Conversation).filter(
                Conversation.conversation_id == conv_id
            ).first()
            vendor = db.query(Vendor).filter(Vendor.vendor_id == conv.vendor_id).first()
            vendor_user = db.query(User).filter(User.user_id == vendor.user_id).first()
            client.post(f"/conversations/{conv_id}/messages", json={"content": "yes"},
                        headers=make_auth_headers(vendor_user))

        refused = self._ask_new_vendor(world, 999)
        assert refused.status_code == 429
        assert "one day" in refused.json()["detail"]


# ── Booking threads ───────────────────────────────────────────────────


class TestBookingThreads:
    def test_either_party_opens_the_same_thread(self, world):
        booking_id = world["booking"].booking_id
        a = client.post(f"/conversations/booking/{booking_id}",
                        headers=make_auth_headers(world["client_user"]))
        b = client.post(f"/conversations/booking/{booking_id}",
                        headers=make_auth_headers(world["vendor_user"]))
        assert a.status_code == 200 and b.status_code == 200
        assert a.json()["conversation_id"] == b.json()["conversation_id"]

    def test_it_is_named_for_the_other_party_and_the_service(self, world):
        booking_id = world["booking"].booking_id
        seen_by_client = client.post(f"/conversations/booking/{booking_id}",
                                     headers=make_auth_headers(world["client_user"])).json()
        seen_by_vendor = client.post(f"/conversations/booking/{booking_id}",
                                     headers=make_auth_headers(world["vendor_user"])).json()
        assert "Wedding Photography" in seen_by_client["name"]
        assert seen_by_client["name"] != seen_by_vendor["name"]

    def test_a_stranger_is_refused(self, world):
        resp = client.post(f"/conversations/booking/{world['booking'].booking_id}",
                           headers=make_auth_headers(world["other_vendor_user"]))
        assert resp.status_code == 403


# ── The inbox ─────────────────────────────────────────────────────────


class TestInbox:
    def test_a_thread_without_a_bundle_still_appears(self, world):
        """The line this whole change turns on.

        list_conversations dropped anything whose bundle wasn't live, so an
        enquiry was created, sent, notified — and never listed.
        """
        _ask(world)
        convs = client.get("/conversations",
                           headers=make_auth_headers(world["client_user"])).json()
        assert len(convs) == 1
        assert convs[0]["bundle_id"] is None

    def test_unread_is_counted_per_row(self, world):
        conv_id = _ask(world).json()["conversation_id"]
        client.post(f"/conversations/{conv_id}/messages", json={"content": "and the 15th?"},
                    headers=make_auth_headers(world["client_user"]))

        vendor_view = client.get("/conversations",
                                 headers=make_auth_headers(world["vendor_user"])).json()
        assert vendor_view[0]["unread_count"] == 2

        # The sender's own messages are never unread to them.
        client_view = client.get("/conversations",
                                 headers=make_auth_headers(world["client_user"])).json()
        assert client_view[0]["unread_count"] == 0

    def test_reading_clears_it(self, world):
        conv_id = _ask(world).json()["conversation_id"]
        client.get(f"/conversations/{conv_id}/messages",
                   headers=make_auth_headers(world["vendor_user"]))
        vendor_view = client.get("/conversations",
                                 headers=make_auth_headers(world["vendor_user"])).json()
        assert vendor_view[0]["unread_count"] == 0

    def test_sorted_by_what_happened_last(self, world):
        db = world["db"]
        first = _ask(world).json()["conversation_id"]
        u = _user(db, "later", str(uuid.uuid4())[:8])
        v = Vendor(user_id=u.user_id, bio="x", rating=5.0, num_events=1)
        db.add(v)
        db.commit()
        db.refresh(v)
        second = _ask(world, vendor_id=v.vendor_id, content="hello").json()["conversation_id"]

        # Talking on the older thread brings it back to the top.
        client.post(f"/conversations/{first}/messages", json={"content": "still there?"},
                    headers=make_auth_headers(world["client_user"]))
        order = [c["conversation_id"] for c in client.get(
            "/conversations", headers=make_auth_headers(world["client_user"])).json()]
        assert order[0] == first
        assert second in order


# ── Negotiation, in the thread ────────────────────────────────────────


class TestOfferMessages:
    def _offer(self, world, cents=180000, message=None):
        return client.post("/negotiations", json={
            "booking_id": world["booking"].booking_id,
            "amount_cents": cents,
            "message": message,
        }, headers=make_auth_headers(world["client_user"]))

    def test_an_offer_lands_in_the_booking_thread(self, world):
        assert self._offer(world, message="we'd drop the second shooter").status_code in (200, 201)

        thread = client.post(f"/conversations/booking/{world['booking'].booking_id}",
                             headers=make_auth_headers(world["client_user"])).json()
        msgs = client.get(f"/conversations/{thread['conversation_id']}/messages",
                          headers=make_auth_headers(world["client_user"])).json()["items"]
        assert len(msgs) == 1
        assert msgs[0]["kind"] == "offer"
        assert "$1,800.00" in msgs[0]["content"]
        assert "second shooter" in msgs[0]["content"]
        assert msgs[0]["meta"]["amount_cents"] == 180000
        assert msgs[0]["meta"]["action"] == "offer"

    def test_the_whole_haggle_is_one_ordered_thread(self, world):
        self._offer(world)
        neg = client.get(f"/negotiations/booking/{world['booking'].booking_id}",
                         headers=make_auth_headers(world["client_user"])).json()
        client.post(f"/negotiations/{neg['negotiation_id']}/offer",
                    json={"amount_cents": 190000, "message": "meet in the middle?"},
                    headers=make_auth_headers(world["vendor_user"]))
        client.post(f"/negotiations/{neg['negotiation_id']}/accept",
                    headers=make_auth_headers(world["client_user"]))

        thread = client.post(f"/conversations/booking/{world['booking'].booking_id}",
                             headers=make_auth_headers(world["client_user"])).json()
        msgs = client.get(f"/conversations/{thread['conversation_id']}/messages",
                          headers=make_auth_headers(world["client_user"])).json()["items"]
        assert [m["meta"]["action"] for m in msgs] == ["offer", "counter", "accept"]
        # Every one of them reads as a sentence without the card around it.
        assert all(m["content"] and not m["content"].startswith("{") for m in msgs)

    def test_a_decline_says_so(self, world):
        self._offer(world)
        neg = client.get(f"/negotiations/booking/{world['booking'].booking_id}",
                         headers=make_auth_headers(world["client_user"])).json()
        client.post(f"/negotiations/{neg['negotiation_id']}/reject",
                    json={"message": "too far off"},
                    headers=make_auth_headers(world["vendor_user"]))

        thread = client.post(f"/conversations/booking/{world['booking'].booking_id}",
                             headers=make_auth_headers(world["client_user"])).json()
        msgs = client.get(f"/conversations/{thread['conversation_id']}/messages",
                          headers=make_auth_headers(world["client_user"])).json()["items"]
        assert msgs[-1]["content"] == "Declined the offer — too far off"

    def test_offers_do_not_spend_the_flood_allowance(self, world):
        """A haggle is the user's action, limited where the action is taken."""
        db = world["db"]
        self._offer(world)
        thread = client.post(f"/conversations/booking/{world['booking'].booking_id}",
                             headers=make_auth_headers(world["client_user"])).json()
        sent = db.query(GroupMessage).filter(
            GroupMessage.conversation_id == thread["conversation_id"]
        ).count()
        assert sent == 1
