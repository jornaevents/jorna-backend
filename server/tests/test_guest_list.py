"""Guest lists, per-function invitations, and what strangers with a link can see.

The rules worth pinning down here are the ones that would be expensive to get
wrong: a headcount that quietly counts the wrong people, and a token that shows
somebody more than their own invitation.
"""

import uuid

import pytest

from app.db.models import Event, EventFunction, Guest, GuestInvite, User
from app.services.guest_service import (
    GuestError,
    add_function,
    add_guest,
    add_guests_bulk,
    delete_function,
    headcount,
    invitation_for_guest,
    invitation_for_open_link,
    join_via_open_link,
    list_functions,
    list_guests,
    open_link_token,
    record_reply,
    retire_open_link,
    update_guest,
)
from tests.test_api import TestingSessionLocal, client, make_auth_headers_from_parts


@pytest.fixture
def host():
    """A client with one celebration, and a second client who owns nothing."""
    db = TestingSessionLocal()
    uid = str(uuid.uuid4())[:8]
    owner = User(
        email=f"g_h_{uid}@t.com", username=f"g_h_{uid}", password="pw", phone="1",
        f_name="H", l_name="O", age=30, location="Chicago, IL", gender="F",
        language="EN", token_version=0,
    )
    stranger = User(
        email=f"g_s_{uid}@t.com", username=f"g_s_{uid}", password="pw", phone="1",
        f_name="S", l_name="T", age=30, location="Chicago, IL", gender="M",
        language="EN", token_version=0,
    )
    db.add_all([owner, stranger])
    db.commit()
    db.refresh(owner)
    db.refresh(stranger)

    event = Event(
        user_id=owner.user_id, name="Sharma Wedding", date_iso="2027-06-05",
        location="12 Maple Ave, Evanston, IL 60201", guest_count=200,
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    out = {
        "user_id": owner.user_id,
        "event_id": event.event_id,
        "headers": make_auth_headers_from_parts(owner.user_id, owner.email, 0),
        "stranger_id": stranger.user_id,
        "stranger_headers": make_auth_headers_from_parts(
            stranger.user_id, stranger.email, 0
        ),
    }
    db.close()
    yield out


def _db():
    return TestingSessionLocal()


# ── Functions ─────────────────────────────────────────────────────────


def test_a_celebration_starts_with_one_function(host):
    """A host who never thinks about functions still has something to invite to."""
    db = _db()
    try:
        functions = list_functions(event_id=host["event_id"], user_id=host["user_id"], db=db)
        assert len(functions) == 1
        assert functions[0]["name"] == "Sharma Wedding"
        # Asking twice doesn't make two.
        again = list_functions(event_id=host["event_id"], user_id=host["user_id"], db=db)
        assert [f["function_id"] for f in again] == [f["function_id"] for f in functions]
    finally:
        db.close()


def test_the_last_function_cannot_be_deleted(host):
    db = _db()
    try:
        only = list_functions(event_id=host["event_id"], user_id=host["user_id"], db=db)[0]
        with pytest.raises(GuestError) as e:
            delete_function(function_id=only["function_id"], user_id=host["user_id"], db=db)
        assert e.value.status_code == 400
    finally:
        db.close()


def test_deleting_a_function_keeps_the_guests(host):
    """Losing a whole guest list because a function was removed would be its own
    kind of disaster."""
    db = _db()
    try:
        base = list_functions(event_id=host["event_id"], user_id=host["user_id"], db=db)[0]
        sangeet = add_function(
            event_id=host["event_id"], user_id=host["user_id"], name="Sangeet",
            date_iso="2027-06-04", time_start="19:00", time_end="23:00",
            location=None, db=db,
        )
        guest = add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Anita", email=None,
            phone=None, party_size=2,
            function_ids=[base["function_id"], sangeet["function_id"]], note=None, db=db,
        )

        delete_function(function_id=sangeet["function_id"], user_id=host["user_id"], db=db)

        still_there = db.query(Guest).filter(Guest.guest_id == guest["guest_id"]).first()
        assert still_there is not None
        remaining = db.query(GuestInvite).filter(GuestInvite.guest_id == guest["guest_id"]).all()
        assert [i.function_id for i in remaining] == [base["function_id"]]
    finally:
        db.close()


# ── The list ──────────────────────────────────────────────────────────


def test_a_guest_with_no_functions_named_is_invited_to_all(host):
    """What a host means by adding somebody without saying which parts."""
    db = _db()
    try:
        base = list_functions(event_id=host["event_id"], user_id=host["user_id"], db=db)[0]
        mehndi = add_function(
            event_id=host["event_id"], user_id=host["user_id"], name="Mehndi",
            date_iso="2027-06-03", time_start=None, time_end=None, location=None, db=db,
        )
        guest = add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Raj", email=None,
            phone=None, party_size=1, function_ids=[], note=None, db=db,
        )
        invited = {i["function_id"] for i in guest["invites"]}
        assert invited == {base["function_id"], mehndi["function_id"]}
    finally:
        db.close()


def test_a_guest_cannot_be_invited_to_another_celebrations_function(host):
    db = _db()
    try:
        other_event = Event(
            user_id=host["stranger_id"], name="Other", date_iso="2027-07-01",
            location="Elsewhere",
        )
        db.add(other_event)
        db.commit()
        db.refresh(other_event)
        theirs = add_function(
            event_id=other_event.event_id, user_id=host["stranger_id"], name="Theirs",
            date_iso=None, time_start=None, time_end=None, location=None, db=db,
        )

        guest = add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Priya", email=None,
            phone=None, party_size=1, function_ids=[theirs["function_id"]], note=None, db=db,
        )
        # Filtered out, so it fell back to this celebration's own functions.
        assert theirs["function_id"] not in {i["function_id"] for i in guest["invites"]}
        assert len(guest["invites"]) == 1
    finally:
        db.close()


def test_bulk_paste_reads_name_email_and_size(host):
    db = _db()
    try:
        result = add_guests_bulk(
            event_id=host["event_id"], user_id=host["user_id"],
            lines=[
                "Anita Sharma, anita@example.com, 3",
                "Raj Patel\traj@example.com\t2",
                "   ",
                "Just A Name",
            ],
            function_ids=[], db=db,
        )
        assert result["added"] == 3
        by_name = {g["name"]: g for g in result["guests"]}
        assert by_name["Anita Sharma"]["email"] == "anita@example.com"
        assert by_name["Anita Sharma"]["party_size"] == 3
        assert by_name["Raj Patel"]["party_size"] == 2
        assert by_name["Just A Name"]["party_size"] == 1
        assert by_name["Just A Name"]["email"] is None
    finally:
        db.close()


def test_editing_a_guests_functions_keeps_replies_already_given(host):
    """Removing somebody from the sangeet and putting them back shouldn't lose
    the fact that they said yes."""
    db = _db()
    try:
        base = list_functions(event_id=host["event_id"], user_id=host["user_id"], db=db)[0]
        sangeet = add_function(
            event_id=host["event_id"], user_id=host["user_id"], name="Sangeet",
            date_iso=None, time_start=None, time_end=None, location=None, db=db,
        )
        guest = add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Anita", email=None,
            phone=None, party_size=4, function_ids=[], note=None, db=db,
        )
        record_reply(
            token=guest["token"],
            replies=[{"function_id": base["function_id"], "status": "attending",
                      "attending_count": 3}],
            db=db,
        )

        update_guest(
            guest_id=guest["guest_id"], user_id=host["user_id"],
            updates={"function_ids": [base["function_id"], sangeet["function_id"]]}, db=db,
        )

        after = invitation_for_guest(token=guest["token"], db=db)
        replies = {r["function_id"]: r for r in after["guest"]["replies"]}
        assert replies[base["function_id"]]["status"] == "attending"
        assert replies[base["function_id"]]["attending_count"] == 3
        assert replies[sangeet["function_id"]]["status"] == "no_reply"
    finally:
        db.close()


def test_a_guest_cannot_be_left_invited_to_nothing(host):
    db = _db()
    try:
        guest = add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Anita", email=None,
            phone=None, party_size=1, function_ids=[], note=None, db=db,
        )
        with pytest.raises(GuestError) as e:
            update_guest(
                guest_id=guest["guest_id"], user_id=host["user_id"],
                updates={"function_ids": []}, db=db,
            )
        assert e.value.status_code == 400
    finally:
        db.close()


# ── The headcount ─────────────────────────────────────────────────────


def test_headcount_counts_people_not_invitations(host):
    """"The Kapoor family, 4" is one line on a list and four plates at a table."""
    db = _db()
    try:
        base = list_functions(event_id=host["event_id"], user_id=host["user_id"], db=db)[0]
        fid = base["function_id"]

        yes_with_number = add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Kapoor family",
            email=None, phone=None, party_size=4, function_ids=[fid], note=None, db=db,
        )
        yes_no_number = add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Mehta family",
            email=None, phone=None, party_size=2, function_ids=[fid], note=None, db=db,
        )
        says_no = add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Rao", email=None,
            phone=None, party_size=3, function_ids=[fid], note=None, db=db,
        )
        add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Silent", email=None,
            phone=None, party_size=5, function_ids=[fid], note=None, db=db,
        )

        # Fewer than invited.
        record_reply(token=yes_with_number["token"],
                     replies=[{"function_id": fid, "status": "attending",
                               "attending_count": 3}], db=db)
        # A yes without a number means the people who were invited.
        record_reply(token=yes_no_number["token"],
                     replies=[{"function_id": fid, "status": "attending"}], db=db)
        record_reply(token=says_no["token"],
                     replies=[{"function_id": fid, "status": "declined"}], db=db)

        counts = {c["function_id"]: c for c in headcount(event_id=host["event_id"], db=db)}[fid]
        assert counts["invited"] == 4
        assert counts["attending"] == 5      # 3 + 2, and nothing from the decline
        assert counts["declined"] == 1
        assert counts["no_reply"] == 1
        assert counts["expected"] == 14      # 4 + 2 + 3 + 5, what the host put down
    finally:
        db.close()


def test_headcount_is_per_function(host):
    """The number a caterer bills against is for their function, not the week."""
    db = _db()
    try:
        base = list_functions(event_id=host["event_id"], user_id=host["user_id"], db=db)[0]
        mehndi = add_function(
            event_id=host["event_id"], user_id=host["user_id"], name="Mehndi",
            date_iso="2027-06-03", time_start=None, time_end=None, location=None, db=db,
        )
        close_family = add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Close family",
            email=None, phone=None, party_size=6, function_ids=[], note=None, db=db,
        )
        add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Colleague", email=None,
            phone=None, party_size=1, function_ids=[base["function_id"]], note=None, db=db,
        )
        record_reply(
            token=close_family["token"],
            replies=[
                {"function_id": base["function_id"], "status": "attending"},
                {"function_id": mehndi["function_id"], "status": "attending",
                 "attending_count": 4},
            ],
            db=db,
        )

        counts = {c["function_id"]: c for c in headcount(event_id=host["event_id"], db=db)}
        assert counts[base["function_id"]]["invited"] == 2
        assert counts[base["function_id"]]["attending"] == 6
        assert counts[mehndi["function_id"]]["invited"] == 1
        assert counts[mehndi["function_id"]]["attending"] == 4
    finally:
        db.close()


# ── Ownership ─────────────────────────────────────────────────────────


def test_another_client_cannot_read_or_touch_the_list(host):
    db = _db()
    try:
        with pytest.raises(GuestError) as e:
            list_guests(event_id=host["event_id"], user_id=host["stranger_id"], db=db)
        assert e.value.status_code == 403

        with pytest.raises(GuestError):
            add_guest(
                event_id=host["event_id"], user_id=host["stranger_id"], name="Gatecrasher",
                email=None, phone=None, party_size=1, function_ids=[], note=None, db=db,
            )
    finally:
        db.close()


def test_the_guests_endpoint_refuses_another_client(host):
    r = client.get(
        f"/events/{host['event_id']}/guests", headers=host["stranger_headers"]
    )
    assert r.status_code == 403


def test_the_guests_endpoint_needs_a_login(host):
    r = client.get(f"/events/{host['event_id']}/guests")
    assert r.status_code in (401, 403)


# ── What a link shows ─────────────────────────────────────────────────


def test_an_invitation_shows_the_reader_and_nobody_else(host):
    """The token is the whole credential, so the response is bounded by what an
    invitation should say."""
    db = _db()
    try:
        base = list_functions(event_id=host["event_id"], user_id=host["user_id"], db=db)[0]
        reader = add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Anita", email=None,
            phone=None, party_size=2, function_ids=[base["function_id"]], note=None, db=db,
        )
        add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Somebody Else",
            email="private@example.com", phone="555-0100", party_size=1,
            function_ids=[base["function_id"]], note=None, db=db,
        )

        invitation = invitation_for_guest(token=reader["token"], db=db)
        body = str(invitation)
        assert invitation["guest"]["name"] == "Anita"
        assert "Somebody Else" not in body
        assert "private@example.com" not in body
        assert "555-0100" not in body
        # Nothing about the host's arrangements.
        assert "guests" not in invitation
        assert "budget" not in body
    finally:
        db.close()


def test_a_guest_only_sees_the_functions_they_are_invited_to(host):
    db = _db()
    try:
        base = list_functions(event_id=host["event_id"], user_id=host["user_id"], db=db)[0]
        add_function(
            event_id=host["event_id"], user_id=host["user_id"], name="Family only mehndi",
            date_iso=None, time_start=None, time_end=None, location=None, db=db,
        )
        guest = add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Colleague", email=None,
            phone=None, party_size=1, function_ids=[base["function_id"]], note=None, db=db,
        )

        invitation = invitation_for_guest(token=guest["token"], db=db)
        assert [f["function_id"] for f in invitation["functions"]] == [base["function_id"]]
        assert "Family only mehndi" not in str(invitation)
    finally:
        db.close()


def test_a_made_up_token_is_a_404():
    db = _db()
    try:
        with pytest.raises(GuestError) as e:
            invitation_for_guest(token="not-a-real-token", db=db)
        assert e.value.status_code == 404
    finally:
        db.close()

    r = client.get("/rsvp/not-a-real-token")
    assert r.status_code == 404


def test_reading_an_invitation_does_not_answer_it(host):
    """Mail scanners and link previews fetch URLs before a person sees them."""
    db = _db()
    try:
        base = list_functions(event_id=host["event_id"], user_id=host["user_id"], db=db)[0]
        guest = add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Anita", email=None,
            phone=None, party_size=2, function_ids=[base["function_id"]], note=None, db=db,
        )
    finally:
        db.close()

    r = client.get(f"/rsvp/{guest['token']}")
    assert r.status_code == 200
    assert r.json()["guest"]["replies"][0]["status"] == "no_reply"

    counts = None
    db = _db()
    try:
        counts = headcount(event_id=host["event_id"], db=db)[0]
    finally:
        db.close()
    assert counts["attending"] == 0
    assert counts["no_reply"] == 1


def test_a_guest_can_change_their_answer(host):
    db = _db()
    try:
        base = list_functions(event_id=host["event_id"], user_id=host["user_id"], db=db)[0]
        fid = base["function_id"]
        guest = add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Anita", email=None,
            phone=None, party_size=4, function_ids=[fid], note=None, db=db,
        )

        record_reply(token=guest["token"],
                     replies=[{"function_id": fid, "status": "attending",
                               "attending_count": 4}], db=db)
        record_reply(token=guest["token"],
                     replies=[{"function_id": fid, "status": "declined"}], db=db)

        counts = headcount(event_id=host["event_id"], db=db)[0]
        assert counts["attending"] == 0
        assert counts["declined"] == 1
    finally:
        db.close()


def test_a_reply_to_a_function_you_were_not_invited_to_is_ignored(host):
    db = _db()
    try:
        base = list_functions(event_id=host["event_id"], user_id=host["user_id"], db=db)[0]
        private = add_function(
            event_id=host["event_id"], user_id=host["user_id"], name="Family only",
            date_iso=None, time_start=None, time_end=None, location=None, db=db,
        )
        guest = add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Colleague", email=None,
            phone=None, party_size=1, function_ids=[base["function_id"]], note=None, db=db,
        )

        record_reply(
            token=guest["token"],
            replies=[
                {"function_id": base["function_id"], "status": "attending"},
                {"function_id": private["function_id"], "status": "attending",
                 "attending_count": 9},
            ],
            db=db,
        )

        counts = {c["function_id"]: c for c in headcount(event_id=host["event_id"], db=db)}
        assert counts[base["function_id"]]["attending"] == 1
        assert counts[private["function_id"]]["invited"] == 0
        assert counts[private["function_id"]]["attending"] == 0
    finally:
        db.close()


# ── The open link ─────────────────────────────────────────────────────


def test_the_open_link_exists_only_once_a_host_asks(host):
    db = _db()
    try:
        event = db.query(Event).filter(Event.event_id == host["event_id"]).first()
        assert event.invite_token is None

        first = open_link_token(event_id=host["event_id"], user_id=host["user_id"], db=db)
        second = open_link_token(event_id=host["event_id"], user_id=host["user_id"], db=db)
        assert first["token"] and first["token"] == second["token"]
    finally:
        db.close()


def test_joining_through_the_open_link_adds_a_guest_who_can_come_back(host):
    db = _db()
    try:
        base = list_functions(event_id=host["event_id"], user_id=host["user_id"], db=db)[0]
        link = open_link_token(event_id=host["event_id"], user_id=host["user_id"], db=db)

        # A stranger with the link sees the celebration but is nobody yet.
        preview = invitation_for_open_link(token=link["token"], db=db)
        assert preview["guest"] is None
        assert preview["event_name"] == "Sharma Wedding"

        joined = join_via_open_link(
            token=link["token"], name="Cousin Ravi", email="ravi@example.com",
            party_size=3,
            replies=[{"function_id": base["function_id"], "status": "attending",
                      "attending_count": 3}],
            db=db,
        )
        assert joined["token"] != link["token"]

        # From here they hold an ordinary invitation.
        mine = invitation_for_guest(token=joined["token"], db=db)
        assert mine["guest"]["name"] == "Cousin Ravi"

        row = db.query(Guest).filter(Guest.token == joined["token"]).first()
        assert row.self_added is True

        counts = headcount(event_id=host["event_id"], db=db)[0]
        assert counts["attending"] == 3
    finally:
        db.close()


def test_someone_joining_is_invited_to_everything_they_did_not_answer(host):
    """Skipping the reception means undecided about it, not uninvited from it."""
    db = _db()
    try:
        base = list_functions(event_id=host["event_id"], user_id=host["user_id"], db=db)[0]
        mehndi = add_function(
            event_id=host["event_id"], user_id=host["user_id"], name="Mehndi",
            date_iso=None, time_start=None, time_end=None, location=None, db=db,
        )
        link = open_link_token(event_id=host["event_id"], user_id=host["user_id"], db=db)

        joined = join_via_open_link(
            token=link["token"], name="Ravi", email=None, party_size=2,
            replies=[{"function_id": base["function_id"], "status": "attending"}], db=db,
        )

        replies = {r["function_id"]: r for r in
                   invitation_for_guest(token=joined["token"], db=db)["guest"]["replies"]}
        assert replies[base["function_id"]]["status"] == "attending"
        assert replies[mehndi["function_id"]]["status"] == "no_reply"
    finally:
        db.close()


def test_retiring_the_open_link_keeps_the_people_it_brought(host):
    db = _db()
    try:
        link = open_link_token(event_id=host["event_id"], user_id=host["user_id"], db=db)
        joined = join_via_open_link(
            token=link["token"], name="Ravi", email=None, party_size=2, replies=[], db=db,
        )

        retire_open_link(event_id=host["event_id"], user_id=host["user_id"], db=db)

        with pytest.raises(GuestError):
            invitation_for_open_link(token=link["token"], db=db)
        # Their own link still works.
        assert invitation_for_guest(token=joined["token"], db=db)["guest"]["name"] == "Ravi"
    finally:
        db.close()


def test_the_rsvp_route_accepts_either_kind_of_link(host):
    """One URL shape, because a guest can't tell which kind they were sent."""
    db = _db()
    try:
        base = list_functions(event_id=host["event_id"], user_id=host["user_id"], db=db)[0]
        guest = add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Anita", email=None,
            phone=None, party_size=2, function_ids=[base["function_id"]], note=None, db=db,
        )
        link = open_link_token(event_id=host["event_id"], user_id=host["user_id"], db=db)
    finally:
        db.close()

    own = client.get(f"/rsvp/{guest['token']}")
    assert own.status_code == 200
    assert own.json()["guest"]["name"] == "Anita"

    shared = client.get(f"/rsvp/{link['token']}")
    assert shared.status_code == 200
    assert shared.json()["guest"] is None


def test_answering_over_http_updates_the_headcount(host):
    db = _db()
    try:
        base = list_functions(event_id=host["event_id"], user_id=host["user_id"], db=db)[0]
        guest = add_guest(
            event_id=host["event_id"], user_id=host["user_id"], name="Anita", email=None,
            phone=None, party_size=4, function_ids=[base["function_id"]], note=None, db=db,
        )
    finally:
        db.close()

    r = client.post(
        f"/rsvp/{guest['token']}",
        json={"replies": [{"function_id": base["function_id"], "status": "attending",
                           "attending_count": 3}]},
    )
    assert r.status_code == 200

    seen = client.get(f"/events/{host['event_id']}/guests", headers=host["headers"])
    assert seen.status_code == 200
    counts = seen.json()["headcount"][0]
    assert counts["attending"] == 3
    assert counts["expected"] == 4
