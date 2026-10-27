"""Messages' status tags and tabs, and hiding plan chats (docs/DECISIONS.md #37).

A two-person thread carries one tag per booking the couple has sent that
vendor — inquiry, negotiation, booked or closed, the Leads page's own words —
and sits under Bookings once there is one, Enquiries until then. Plan chats
hide from the inbox, the badge and the digest while PLAN_CHATS_ENABLED is off.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import config as app_config
from app.db.models import Booking, Bundle, Service, User, Vendor
from app.services import conversation_service as cs
from app.services import message_digest_service as mds
from app.services.pipeline_service import booking_stage
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
    """A client, two vendors, and a package from each."""
    db = TestingSessionLocal()
    uid = uuid.uuid4().hex[:8]
    client_user = _user(db, "tagclient", uid)
    vendor_user = _user(db, "tagvendor", uid)
    other_vendor_user = _user(db, "tagvendortwo", uid)
    vendor = Vendor(user_id=vendor_user.user_id, bio="Photos", rating=4.9, num_events=10)
    other_vendor = Vendor(user_id=other_vendor_user.user_id, bio="DJ", rating=4.7, num_events=5)
    db.add_all([vendor, other_vendor])
    db.commit()
    gold = Service(name="Gold Photography", price=2000.0, duration_minutes=480,
                   vendor_id=vendor.vendor_id, experience="8 years")
    mehndi = Service(name="Mehndi Coverage", price=900.0, duration_minutes=240,
                     vendor_id=vendor.vendor_id, experience="8 years")
    dj = Service(name="DJ Night", price=1500.0, duration_minutes=300,
                 vendor_id=other_vendor.vendor_id, experience="5 years")
    db.add_all([gold, mehndi, dj])
    db.commit()
    for row in (vendor, other_vendor, gold, mehndi, dj):
        db.refresh(row)
    yield {
        "db": db, "client_user": client_user, "vendor_user": vendor_user,
        "vendor": vendor, "other_vendor": other_vendor,
        "gold": gold, "mehndi": mehndi, "dj": dj,
    }
    db.close()


def _booking(world, service=None, **fields):
    service = service or world["gold"]
    b = Booking(
        user_id=world["client_user"].user_id, vendor_id=service.vendor_id,
        service_id=service.service_id, time_start="10:00", time_end="18:00",
        location="Hall", date_iso="2027-06-14", **{"status": "pending", **fields},
    )
    world["db"].add(b)
    world["db"].commit()
    world["db"].refresh(b)
    return b


def _bundle(world, status):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    bundle = Bundle(user_id=world["client_user"].user_id, name="Sharma Wedding",
                    status=status, created_at=now, updated_at=now)
    world["db"].add(bundle)
    world["db"].commit()
    world["db"].refresh(bundle)
    return bundle


def _ask(world, vendor=None):
    resp = client.post("/conversations/enquiry", json={
        "vendor_id": (vendor or world["vendor"]).vendor_id, "content": "Are you free on June 14?",
    }, headers=make_auth_headers(world["client_user"]))
    assert resp.status_code == 201, resp.text
    return resp.json()["conversation_id"]


def _inbox(user):
    resp = client.get("/conversations", headers=make_auth_headers(user))
    assert resp.status_code == 200, resp.text
    return {c["conversation_id"]: c for c in resp.json()}


# ── One booking's stage ───────────────────────────────────────────────


class TestBookingStage:
    """Bare rows, so every state is reached without walking the whole flow."""

    def _row(self, **fields):
        return Booking(**{"status": "pending", **fields})

    def test_a_request_with_no_contract_is_an_inquiry(self):
        assert booking_stage(self._row()) == "inquiry"

    def test_a_draft_contract_is_still_an_inquiry(self):
        assert booking_stage(self._row(contract_token="t", contract_status="draft")) == "inquiry"

    @pytest.mark.parametrize("status", ["sent", "viewed", "declined", "awaiting_vendor"])
    def test_a_contract_with_the_couple_is_a_negotiation(self, status):
        assert booking_stage(self._row(contract_token="t", contract_status=status)) == "negotiation"

    def test_a_lapsed_hold_is_still_a_negotiation(self):
        """Leads shows an expired offer as needing the vendor, not as closed."""
        row = self._row(
            contract_token="t", contract_status="sent",
            hold_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1),
        )
        assert booking_stage(row) == "negotiation"

    def test_a_countersigned_contract_is_booked(self):
        row = self._row(contract_token="t", contract_status="signed",
                        signed_at=datetime(2027, 1, 1), status="approved")
        assert booking_stage(row) == "booked"

    @pytest.mark.parametrize("status", ["approved", "payment_confirmed"])
    def test_an_accepted_request_from_before_contracts_is_booked(self, status):
        assert booking_stage(self._row(status=status)) == "booked"

    def test_a_declined_request_is_closed(self):
        assert booking_stage(self._row(status="rejected", rejected_reason="vendor_declined")) == "closed"

    def test_a_cancelled_booking_is_closed_even_after_signing(self):
        row = self._row(status="approved", signed_at=datetime(2027, 1, 1),
                        cancelled_at=datetime(2027, 2, 1))
        assert booking_stage(row) == "closed"

    def test_a_voided_contract_is_closed(self):
        assert booking_stage(self._row(contract_token="t", contract_status="voided")) == "closed"


# ── Tags and tabs on threads ──────────────────────────────────────────


class TestThreadTags:
    def test_a_question_with_no_booking_is_an_enquiry(self, world):
        conv_id = _ask(world)
        conv = _inbox(world["client_user"])[conv_id]
        assert conv["tab"] == "enquiries"
        assert conv["tags"] == []

    def test_a_booking_request_moves_the_same_thread_to_bookings(self, world):
        conv_id = _ask(world)
        booking = _booking(world)

        for user in (world["client_user"], world["vendor_user"]):
            conv = _inbox(user)[conv_id]
            assert conv["tab"] == "bookings"
            assert conv["tags"] == [{
                "booking_id": booking.booking_id, "stage": "inquiry",
                "service_id": world["gold"].service_id, "service_name": "Gold Photography",
            }]

    def test_the_tag_follows_the_booking(self, world):
        conv_id = _ask(world)
        booking = _booking(world)
        db = world["db"]

        booking.contract_token = f"tok-{uuid.uuid4().hex}"
        booking.contract_status = "sent"
        db.commit()
        assert _inbox(world["vendor_user"])[conv_id]["tags"][0]["stage"] == "negotiation"

        booking.contract_status = "signed"
        booking.signed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        booking.status = "approved"
        db.commit()
        assert _inbox(world["vendor_user"])[conv_id]["tags"][0]["stage"] == "booked"

        booking.cancelled_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        conv = _inbox(world["vendor_user"])[conv_id]
        assert conv["tags"][0]["stage"] == "closed"
        # A closed booking was still a booking request: it stays under Bookings.
        assert conv["tab"] == "bookings"

    def test_two_events_get_a_tag_each_oldest_first(self, world):
        conv_id = _ask(world)
        first = _booking(world, created_at=datetime(2026, 1, 1))
        second = _booking(world, service=world["mehndi"], created_at=datetime(2026, 2, 1))

        tags = _inbox(world["vendor_user"])[conv_id]["tags"]
        assert [t["booking_id"] for t in tags] == [first.booking_id, second.booking_id]
        assert [t["service_name"] for t in tags] == ["Gold Photography", "Mehndi Coverage"]

    def test_a_booking_with_another_vendor_is_not_this_threads(self, world):
        conv_id = _ask(world)
        _booking(world, service=world["dj"])
        assert _inbox(world["client_user"])[conv_id]["tags"] == []

    def test_a_request_on_an_unsent_plan_does_not_count_yet(self, world):
        """The vendor hasn't been asked until the plan is sent (sent_to_vendor)."""
        draft = _bundle(world, "draft")
        conv_id = _ask(world)
        _booking(world, bundle_id=draft.bundle_id)

        conv = _inbox(world["vendor_user"])[conv_id]
        assert (conv["tab"], conv["tags"]) == ("enquiries", [])

    def test_the_booking_thread_carries_tags_too(self, world):
        booking = _booking(world)
        resp = client.post(f"/conversations/booking/{booking.booking_id}",
                           headers=make_auth_headers(world["client_user"]))
        assert resp.status_code == 200, resp.text
        assert resp.json()["tab"] == "bookings"
        assert [t["booking_id"] for t in resp.json()["tags"]] == [booking.booking_id]

    def test_one_conversation_read_matches_the_inbox(self, world):
        conv_id = _ask(world)
        _booking(world)
        one = client.get(f"/conversations/{conv_id}", headers=make_auth_headers(world["vendor_user"]))
        assert one.status_code == 200
        listed = _inbox(world["vendor_user"])[conv_id]
        assert (one.json()["tab"], one.json()["tags"]) == (listed["tab"], listed["tags"])


# ── Plan chats ────────────────────────────────────────────────────────


@pytest.fixture
def plan_chat(world):
    """A sent plan with the vendor on it, and its two group chats."""
    db = world["db"]
    booking = _booking(world, status="approved")
    bundle = _bundle(world, "confirmed")
    booking.bundle_id = bundle.bundle_id
    db.commit()
    convs = cs.create_bundle_conversations(
        bundle_id=bundle.bundle_id, client_user_id=world["client_user"].user_id, db=db,
    )
    all_parties = next(c["conversation_id"] for c in convs if c["type"] == "all_parties")
    cs.send_group_message(
        conversation_id=all_parties, content="Can photos start at 3:30?",
        caller_user_id=world["client_user"].user_id, db=db,
    )
    return {"ids": {c["conversation_id"] for c in convs}, "all_parties": all_parties}


@pytest.fixture
def plan_chats_off(monkeypatch):
    monkeypatch.setattr(app_config, "PLAN_CHATS_ENABLED", False)


class TestPlanChats:
    def test_on_by_default_they_are_listed_as_before(self, world, plan_chat):
        listed = _inbox(world["vendor_user"])
        assert plan_chat["ids"] <= set(listed)
        assert listed[plan_chat["all_parties"]]["tab"] == "bookings"
        assert listed[plan_chat["all_parties"]]["tags"] == []

    def test_off_they_leave_the_inbox(self, world, plan_chat, plan_chats_off):
        conv_id = _ask(world)
        listed = _inbox(world["vendor_user"])
        assert not plan_chat["ids"] & set(listed)
        assert conv_id in listed

    def test_off_they_leave_the_unread_badge(self, world, plan_chat, monkeypatch):
        headers = make_auth_headers(world["vendor_user"])
        on = client.get("/conversations/unread-count", headers=headers).json()["unread_count"]
        assert on == 1
        monkeypatch.setattr(app_config, "PLAN_CHATS_ENABLED", False)
        off = client.get("/conversations/unread-count", headers=headers).json()["unread_count"]
        assert off == 0

    def test_off_they_leave_the_digest(self, world, plan_chat, plan_chats_off, mocker):
        sender = mocker.patch(
            "app.services.message_digest_service.send_email",
            return_value={"success": True, "id": "e1"},
        )
        assert mds.send_message_digest(world["vendor_user"], world["db"]) is False
        sender.assert_not_called()

    def test_off_they_are_kept_not_deleted(self, world, plan_chat, plan_chats_off):
        """Hidden, not gone: still there by id, so turning the flag on restores them."""
        resp = client.get(f"/conversations/{plan_chat['all_parties']}",
                          headers=make_auth_headers(world["vendor_user"]))
        assert resp.status_code == 200
