"""The label a client sees on a chat with one vendor (app/services/message_labels.py).

Two halves: the rule itself, on bookings built in memory, and the label as
the inbox and a single chat return it, for the client and for the vendor.
The rule mirrors the vendor web app's relationFor (jorna-website,
apps/vendor/web/src/lib/messageLabels.ts); the cases below are its cases.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.db.models import Booking, Bundle, Conversation, Lead
from app.services import message_labels as ml
from app.services.pipeline_service import list_pipeline
from tests.test_api import client, make_auth_headers
from tests.test_conversation_subjects import _ask, world  # noqa: F401 — world is a fixture

TODAY = date(2026, 6, 1)
T = TODAY.isoformat()


def _day(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


def _b(bid: str, *, status="pending", date_iso=None, created=0, **fields) -> Booking:
    """A booking in memory. `created` is minutes after a fixed start, so
    recency is explicit."""
    return Booking(
        booking_id=bid, status=status, date_iso=date_iso or _day(30),
        payment_status=fields.pop("payment_status", "unpaid"),
        created_at=datetime(2026, 1, 1) + timedelta(minutes=created),
        **fields,
    )


def _contract(bid, contract_status, **fields) -> Booking:
    return _b(bid, contract_token=f"tok-{bid}", contract_status=contract_status, **fields)


def _signed(bid, **fields) -> Booking:
    return _contract(bid, "signed", signed_at=datetime(2026, 2, 1), **fields)


def _label(bookings, own=None):
    found = ml.label_for(Conversation(booking_id=own), bookings, T)
    return (found[0], found[1].booking_id) if found else None


# ── The rule ──────────────────────────────────────────────────────────


class TestEachLabel:
    def test_nothing_booked_has_no_label(self):
        assert _label([]) is None

    def test_a_request_is_an_inquiry(self):
        assert _label([_b("r")]) == ("inquiry", "r")

    def test_a_request_being_haggled_over_is_still_an_inquiry(self):
        # The Leads page only moves a lead to Negotiation once a contract is sent.
        assert _label([_b("r", status="negotiation_ongoing")]) == ("inquiry", "r")

    def test_a_draft_contract_is_an_inquiry(self):
        assert _label([_contract("c", "draft")]) == ("inquiry", "c")

    @pytest.mark.parametrize("status", ["sent", "viewed", "expired", "awaiting_vendor"])
    def test_a_sent_contract_is_a_negotiation(self, status):
        assert _label([_contract("c", status)]) == ("negotiation", "c")

    def test_a_signed_contract_is_booked(self):
        assert _label([_signed("s")]) == ("booked", "s")

    @pytest.mark.parametrize("status", ["approved", "payment_confirmed"])
    def test_an_approved_request_from_before_contracts_is_booked(self, status):
        assert _label([_b("a", status=status)]) == ("booked", "a")

    def test_a_contract_with_only_the_clients_signature_is_not_booked(self):
        b = _contract("c", "awaiting_vendor", client_signed_at=datetime(2026, 2, 1))
        assert _label([b]) == ("negotiation", "c")

    def test_a_request_the_vendor_declined_is_closed(self):
        b = _b("d", status="rejected", rejected_reason="vendor_declined")
        assert _label([b]) == ("closed", "d")

    def test_a_contract_the_client_declined_is_closed(self):
        b = _contract("d", "declined", status="rejected", rejected_reason="client_declined")
        assert _label([b]) == ("closed", "d")

    def test_a_request_the_client_cancelled_is_closed(self):
        b = _b("x", status="rejected", rejected_reason="client_cancelled",
               cancelled_at=datetime(2026, 2, 1))
        assert _label([b]) == ("closed", "x")

    def test_a_refunded_booking_is_closed(self):
        b = _b("p", status="payment_confirmed", payment_status="refunded")
        assert _label([b]) == ("closed", "p")

    def test_a_refunded_signed_contract_is_closed_not_booked(self):
        b = _signed("s", payment_status="refunded")
        assert _label([b]) == ("closed", "s")

    def test_a_contract_the_vendor_withdrew_is_closed(self):
        # It leaves the vendor's Leads page, but their Messages still shows
        # the booking as called off, so the client sees the same.
        b = _contract("w", "voided", status="rejected", rejected_reason="vendor_withdrew")
        assert _label([b]) == ("closed", "w")


class TestPriority:
    def test_the_chats_own_booking_wins(self):
        bookings = [_signed("other", date_iso=_day(5)), _signed("own", date_iso=_day(90))]
        assert _label(bookings, own="own") == ("booked", "own")

    def test_the_chats_own_past_booking_still_wins(self):
        bookings = [_b("new"), _signed("own", date_iso=_day(-30))]
        assert _label(bookings, own="own") == ("booked", "own")

    def test_an_upcoming_booking_beats_an_open_request(self):
        bookings = [_b("request", created=50), _signed("booked", date_iso=_day(10))]
        assert _label(bookings) == ("booked", "booked")

    def test_the_soonest_upcoming_booking_wins(self):
        bookings = [_signed("later", date_iso=_day(40)), _signed("sooner", date_iso=_day(4))]
        assert _label(bookings) == ("booked", "sooner")

    def test_an_event_happening_today_is_still_upcoming(self):
        bookings = [_b("request"), _signed("today", date_iso=T)]
        assert _label(bookings) == ("booked", "today")

    def test_a_multi_day_event_is_upcoming_until_its_last_day(self):
        b = _signed("long", date_iso=_day(-2), date_end=_day(1))
        assert _label([_b("request"), b]) == ("booked", "long")

    def test_a_new_request_after_a_past_event_shows_the_request(self):
        bookings = [_signed("past", date_iso=_day(-60)), _contract("next", "sent")]
        assert _label(bookings) == ("negotiation", "next")

    def test_a_past_booking_beats_a_closed_request(self):
        bookings = [
            _b("declined", status="rejected", rejected_reason="vendor_declined", created=99),
            _signed("past", date_iso=_day(-60)),
        ]
        assert _label(bookings) == ("booked", "past")

    def test_the_latest_past_booking_wins(self):
        bookings = [_signed("old", date_iso=_day(-400)), _signed("recent", date_iso=_day(-20))]
        assert _label(bookings) == ("booked", "recent")

    def test_the_chats_own_request_beats_another_open_one(self):
        bookings = [_contract("other", "sent", created=99), _b("own", created=1)]
        assert _label(bookings, own="own") == ("inquiry", "own")

    def test_a_negotiation_beats_an_inquiry(self):
        bookings = [_b("inquiry", created=99), _contract("contract", "viewed", created=1)]
        assert _label(bookings) == ("negotiation", "contract")

    def test_otherwise_the_most_recently_touched_request_wins(self):
        older = _b("older", created=1)
        newer = _b("newer", created=2)
        assert _label([older, newer]) == ("inquiry", "newer")
        # A contract sent later counts as touched later.
        resent = _contract("resent", "draft", created=0, sent_at=datetime(2026, 3, 1))
        assert _label([older, newer, resent]) == ("inquiry", "resent")

    def test_an_open_request_beats_one_that_ended(self):
        bookings = [
            _b("declined", status="rejected", rejected_reason="vendor_declined", created=99),
            _b("open", created=1),
        ]
        assert _label(bookings) == ("inquiry", "open")

    def test_the_most_recent_ending_wins_among_closed(self):
        bookings = [
            _b("first", status="rejected", rejected_reason="vendor_declined", created=1),
            _b("second", status="rejected", rejected_reason="vendor_declined", created=2),
        ]
        assert _label(bookings) == ("closed", "second")

    def test_the_chats_own_closed_booking_wins_among_closed(self):
        bookings = [
            _b("own", status="rejected", rejected_reason="vendor_declined", created=1),
            _b("other", status="rejected", rejected_reason="vendor_declined", created=2),
        ]
        assert _label(bookings, own="own") == ("closed", "own")


class TestSameStageAsTheLeadsPage:
    """For every open request, the client's label is the stage the vendor's
    Leads page gives it, from pipeline_service itself."""

    @pytest.mark.parametrize("fields,stage", [
        ({"status": "pending"}, "inquiry"),
        ({"status": "negotiation_ongoing"}, "inquiry"),
        ({"contract_token": "t-draft", "contract_status": "draft"}, "inquiry"),
        ({"contract_token": "t-sent", "contract_status": "sent"}, "negotiation"),
        ({"contract_token": "t-viewed", "contract_status": "viewed"}, "negotiation"),
        ({"contract_token": "t-await", "contract_status": "awaiting_vendor"}, "negotiation"),
    ])
    def test_matches_the_pipeline(self, world, fields, stage):  # noqa: F811
        db = world["db"]
        b = world["booking"]
        for k, v in fields.items():
            setattr(b, k, f"{v}-{b.booking_id}" if k == "contract_token" else v)
        db.commit()

        items = list_pipeline(caller_user_id=world["vendor_user"].user_id, db=db)["items"]
        pipeline_stage = next(i["stage"] for i in items if i["booking_id"] == b.booking_id)
        found = ml.label_for(Conversation(), [b], date.today().isoformat())
        assert (found[0], pipeline_stage) == (stage, stage)


# ── The label on the inbox ────────────────────────────────────────────


def _inbox(user):
    resp = client.get("/conversations", headers=make_auth_headers(user))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _one(user, conv_id):
    resp = client.get(f"/conversations/{conv_id}", headers=make_auth_headers(user))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _row(user, conv_id):
    return next(c for c in _inbox(user) if c["conversation_id"] == conv_id)


def _booking(world, vendor=None, user=None, **fields):  # noqa: F811
    db = world["db"]
    vendor = vendor or world["vendor"]
    fields.setdefault("date_iso", (date.today() + timedelta(days=60)).isoformat())
    fields.setdefault("status", "pending")
    b = Booking(
        user_id=(user or world["client_user"]).user_id, vendor_id=vendor.vendor_id,
        service_id=world["service"].service_id, time_start="10:00", time_end="18:00",
        location="Hall", **fields,
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def _bundle(world, status):  # noqa: F811
    now = datetime.now(timezone.utc)
    bundle = Bundle(user_id=world["client_user"].user_id, name="Wedding",
                    status=status, created_at=now, updated_at=now)
    world["db"].add(bundle)
    world["db"].commit()
    world["db"].refresh(bundle)
    return bundle


class TestTheInbox:
    def test_a_question_thread_shows_the_request_it_led_to(self, world):  # noqa: F811
        conv_id = _ask(world).json()["conversation_id"]
        label = _row(world["client_user"], conv_id)["label"]
        assert label == {
            "stage": "inquiry",
            "booking_id": world["booking"].booking_id,
            "service_name": "Wedding Photography",
        }

    def test_the_vendor_gets_no_label_from_the_backend(self, world):  # noqa: F811
        # Their app works out its own, from Leads and Bookings.
        conv_id = _ask(world).json()["conversation_id"]
        assert _row(world["vendor_user"], conv_id)["label"] is None
        assert _one(world["vendor_user"], conv_id)["label"] is None

    def test_a_single_chat_says_the_same_as_the_inbox(self, world):  # noqa: F811
        conv_id = _ask(world).json()["conversation_id"]
        assert _one(world["client_user"], conv_id)["label"] == _row(world["client_user"], conv_id)["label"]

    def test_the_label_follows_the_booking(self, world):  # noqa: F811
        db = world["db"]
        conv_id = _ask(world).json()["conversation_id"]
        b = world["booking"]

        b.contract_token, b.contract_status = f"tok-{b.booking_id}", "sent"
        db.commit()
        assert _row(world["client_user"], conv_id)["label"]["stage"] == "negotiation"

        b.contract_status, b.signed_at = "signed", datetime(2026, 1, 1)
        db.commit()
        assert _row(world["client_user"], conv_id)["label"]["stage"] == "booked"

    def test_a_declined_request_reads_closed(self, world):  # noqa: F811
        db = world["db"]
        conv_id = _ask(world).json()["conversation_id"]
        world["booking"].status = "rejected"
        world["booking"].rejected_reason = "vendor_declined"
        db.commit()
        assert _row(world["client_user"], conv_id)["label"]["stage"] == "closed"

    def test_a_question_with_nothing_booked_has_no_label(self, world):  # noqa: F811
        world["db"].delete(world["booking"])
        world["db"].commit()
        conv_id = _ask(world).json()["conversation_id"]
        assert _row(world["client_user"], conv_id)["label"] is None

    def test_a_booking_thread_shows_its_own_booking(self, world):  # noqa: F811
        other = _booking(world, contract_token="tok-x", contract_status="sent")
        resp = client.post(f"/conversations/booking/{world['booking'].booking_id}",
                           headers=make_auth_headers(world["client_user"]))
        conv_id = resp.json()["conversation_id"]
        label = _row(world["client_user"], conv_id)["label"]
        assert (label["stage"], label["booking_id"]) == ("inquiry", world["booking"].booking_id)
        assert other.booking_id != label["booking_id"]

    def test_an_older_booking_thread_without_its_client_recorded(self, world):  # noqa: F811
        db = world["db"]
        conv_id = client.post(
            f"/conversations/booking/{world['booking'].booking_id}",
            headers=make_auth_headers(world["client_user"]),
        ).json()["conversation_id"]
        conv = db.query(Conversation).filter(Conversation.conversation_id == conv_id).first()
        conv.client_user_id = None
        db.commit()
        assert _row(world["client_user"], conv_id)["label"]["stage"] == "inquiry"
        assert _row(world["vendor_user"], conv_id)["label"] is None

    def test_a_request_on_an_unsent_plan_has_no_label(self, world):  # noqa: F811
        db = world["db"]
        conv_id = _ask(world).json()["conversation_id"]
        bundle = _bundle(world, "draft")
        world["booking"].bundle_id = bundle.bundle_id
        db.commit()
        assert _row(world["client_user"], conv_id)["label"] is None

        bundle.status = "confirmed"
        db.commit()
        assert _row(world["client_user"], conv_id)["label"]["stage"] == "inquiry"

    def test_bookings_with_another_vendor_dont_count(self, world):  # noqa: F811
        db = world["db"]
        world["db"].delete(world["booking"])
        db.commit()
        _booking(world, vendor=world["vendor2"], status="approved")
        conv_id = _ask(world).json()["conversation_id"]
        assert _row(world["client_user"], conv_id)["label"] is None

    def test_another_clients_bookings_dont_count(self, world):  # noqa: F811
        db = world["db"]
        world["booking"].user_id = world["other_vendor_user"].user_id
        db.commit()
        conv_id = _ask(world).json()["conversation_id"]
        assert _row(world["client_user"], conv_id)["label"] is None

    def test_a_vendors_own_lead_isnt_shown_to_the_client(self, world):  # noqa: F811
        db = world["db"]
        world["db"].delete(world["booking"])
        db.commit()
        conv_id = _ask(world).json()["conversation_id"]
        resp = client.post(f"/conversations/{conv_id}/lead",
                           headers=make_auth_headers(world["vendor_user"]))
        assert resp.status_code == 201, resp.text
        assert db.query(Lead).filter(Lead.conversation_id == conv_id).count() == 1
        assert _row(world["client_user"], conv_id)["label"] is None

    def test_each_vendor_chat_gets_its_own_label(self, world):  # noqa: F811
        first = _ask(world).json()["conversation_id"]
        _booking(world, vendor=world["vendor2"], status="approved")
        second = _ask(world, vendor_id=world["vendor2"].vendor_id).json()["conversation_id"]
        rows = {c["conversation_id"]: c["label"] for c in _inbox(world["client_user"])}
        assert rows[first]["stage"] == "inquiry"
        assert rows[second]["stage"] == "booked"

    def test_plan_chats_have_no_label(self, world):  # noqa: F811
        from app.services.conversation_service import create_bundle_conversations

        db = world["db"]
        bundle = _bundle(world, "confirmed")
        world["booking"].bundle_id = bundle.bundle_id
        db.commit()
        create_bundle_conversations(bundle_id=bundle.bundle_id,
                                    client_user_id=world["client_user"].user_id, db=db)
        plan_chats = [c for c in _inbox(world["client_user"]) if c["subject_type"] == "bundle"]
        assert plan_chats  # the client is in "All parties", not "Vendors only"
        assert all(c["label"] is None for c in plan_chats)

    def test_the_whole_inbox_costs_two_queries(self, world, monkeypatch):  # noqa: F811
        """Not one query per chat: three vendor chats, still two lookups."""
        _ask(world)
        _ask(world, vendor_id=world["vendor2"].vendor_id)
        db = world["db"]
        convs = db.query(Conversation).filter(
            Conversation.client_user_id == world["client_user"].user_id
        ).all()
        conv = convs[0]
        convs.append(Conversation(
            conversation_id="old-booking-thread", subject_type="booking",
            booking_id=world["booking"].booking_id, vendor_id=world["vendor"].vendor_id,
            client_user_id=None, type="direct", name="",
        ))

        from sqlalchemy import event
        engine = db.get_bind()
        statements = []

        def count(*_args):
            statements.append(1)

        event.listen(engine, "before_cursor_execute", count)
        try:
            labels = ml.client_labels(convs, world["client_user"].user_id, db)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert len(statements) == 2
        assert labels[conv.conversation_id]["stage"] == "inquiry"
