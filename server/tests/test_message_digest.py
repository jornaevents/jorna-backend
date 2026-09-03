"""The periodic "what you missed" digest email.

A message lands in exactly one digest — the next sweep after it arrives —
tracked per user via User.last_message_digest_at. Read before the sweep
runs and it's excluded; read after, it isn't repeated in a later one either.

Most of this exercises send_message_digest (one user) rather than
send_due_digests (every user with unread messages, DB-wide): the test
sqlite database is shared across every test module and nothing here cleans
up conversation rows other files leave behind (same as
test_conversation_subjects.py's own fixture), so a sweep-all call picks up
whatever other tests happened to leave unread — an exact count from it isn't
meaningful outside a single, freshly-created world. send_due_digests gets
one test of its own, scoped to checking our own user is among its calls
rather than asserting a total.
"""

import uuid

import pytest

from app.db.models import User, Vendor
from app.services import conversation_service as cs
from app.services import message_digest_service as mds
from tests.test_api import TestingSessionLocal


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
def chat_world():
    """A client, a vendor, and an enquiry thread between them — nothing sent yet."""
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]

    client_user = _user(db, "digestclient", uid)
    vendor_user = _user(db, "digestvendor", uid)
    vendor = Vendor(user_id=vendor_user.user_id, bio="Photos", rating=4.9, num_events=10)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    yield {"db": db, "client_user": client_user, "vendor_user": vendor_user, "vendor": vendor}

    # Real rows in a DB shared across test modules — same cleanup discipline
    # as test_conversation_subjects.py's `world` fixture (which also doesn't
    # bother; unique per-test emails mean nothing collides).
    db.close()


def _thread(world, content="Are you free on the 14th?"):
    result = cs.open_enquiry(
        vendor_id=world["vendor"].vendor_id, content=content,
        caller_user_id=world["client_user"].user_id, db=world["db"],
    )
    return result["conversation_id"]


def _mock_send(mocker, **overrides):
    result = {"success": True, "id": "e1"}
    result.update(overrides)
    return mocker.patch("app.services.message_digest_service.send_email", return_value=result)


class TestOneUsersDigest:
    def test_recipient_gets_a_digest_of_the_unread_message(self, chat_world, mocker):
        sender = _mock_send(mocker)
        _thread(chat_world)

        went = mds.send_message_digest(chat_world["vendor_user"], chat_world["db"])
        assert went is True
        sender.assert_called_once()
        assert sender.call_args.kwargs["to"] == chat_world["vendor_user"].email

    def test_the_sender_has_nothing_to_digest(self, chat_world, mocker):
        """Their own message was auto-marked read for them at send time."""
        sender = _mock_send(mocker)
        _thread(chat_world)

        went = mds.send_message_digest(chat_world["client_user"], chat_world["db"])
        assert went is False
        sender.assert_not_called()

    def test_no_unread_means_no_email(self, chat_world, mocker):
        sender = _mock_send(mocker)
        db = chat_world["db"]
        conv_id = _thread(chat_world)
        cs.get_group_messages(
            conversation_id=conv_id, caller_user_id=chat_world["vendor_user"].user_id, db=db,
        )
        went = mds.send_message_digest(chat_world["vendor_user"], db)
        assert went is False
        sender.assert_not_called()

    def test_a_message_already_read_is_not_digested(self, chat_world, mocker):
        sender = _mock_send(mocker)
        db = chat_world["db"]
        conv_id = _thread(chat_world)
        # The vendor reads it in the app before the sweep ever runs.
        cs.get_group_messages(
            conversation_id=conv_id, caller_user_id=chat_world["vendor_user"].user_id, db=db,
        )
        went = mds.send_message_digest(chat_world["vendor_user"], db)
        assert went is False
        sender.assert_not_called()

    def test_a_message_is_only_ever_in_one_digest(self, chat_world, mocker):
        sender = _mock_send(mocker)
        db = chat_world["db"]
        _thread(chat_world)
        vendor = chat_world["vendor_user"]

        assert mds.send_message_digest(vendor, db) is True
        # Still unread, but it already had its one digest.
        assert mds.send_message_digest(vendor, db) is False
        assert sender.call_count == 1

    def test_a_message_after_the_first_digest_gets_its_own(self, chat_world, mocker):
        sender = _mock_send(mocker)
        db = chat_world["db"]
        conv_id = _thread(chat_world)
        vendor = chat_world["vendor_user"]

        mds.send_message_digest(vendor, db)
        cs.send_group_message(
            conversation_id=conv_id, content="Still there?",
            caller_user_id=chat_world["client_user"].user_id, db=db,
        )
        assert mds.send_message_digest(vendor, db) is True
        assert sender.call_count == 2

    def test_a_failed_send_still_advances_the_watermark(self, chat_world, mocker):
        """Same contract as the check-in reminder sweep: a bad address isn't
        retried every pass — the digest window is short enough that a miss
        just rolls into the next one."""
        sender = _mock_send(mocker, success=False, error="boom")
        db = chat_world["db"]
        _thread(chat_world)
        vendor = chat_world["vendor_user"]

        assert mds.send_message_digest(vendor, db) is False  # tried, didn't deliver
        assert sender.call_count == 1
        assert vendor.last_message_digest_at is not None

        # Not retried on the next pass either.
        mds.send_message_digest(vendor, db)
        assert sender.call_count == 1

    def test_the_email_names_the_conversation_and_links_to_it(self, chat_world, mocker):
        sender = _mock_send(mocker)
        conv_id = _thread(chat_world)
        mds.send_message_digest(chat_world["vendor_user"], chat_world["db"])

        html = sender.call_args.kwargs["html"]
        assert conv_id in html


class TestTheSweep:
    def test_a_user_with_unread_messages_is_swept_up(self, chat_world, mocker):
        """send_due_digests iterates every conversation member DB-wide, so this
        only checks our own vendor is among what it sends — not a total count,
        which other tests' leftover rows in the shared test DB would pollute."""
        sender = _mock_send(mocker)
        _thread(chat_world)

        mds.send_due_digests(db=chat_world["db"])

        recipients = {c.kwargs["to"] for c in sender.call_args_list}
        assert chat_world["vendor_user"].email in recipients
        assert chat_world["client_user"].email not in recipients
