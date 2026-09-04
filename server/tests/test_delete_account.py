"""Closing an account, and what it refuses to take with it.

delete_user removed bookings and the vendor row and then went for the user,
while fifteen-odd tables carry a foreign key to users.user_id. It never worked
for anyone: logging in writes a refresh token, and that alone was enough for the
delete to die on the constraint and surface as a 500 with the account intact.
The first test here is that account — nothing but a login behind it.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import (
    Booking,
    Bundle,
    Conversation,
    ConversationMember,
    Event,
    GroupMessage,
    Message,
    RefreshToken,
    Review,
    Service,
    User,
    Vendor,
    VendorAvailability,
)
from app.services.user_service import UserError, delete_user
from tests.test_api import TestingSessionLocal


def _user(db, tag: str) -> User:
    u = User(
        email=f"{tag}@t.com", username=tag, password="pw", phone="1",
        f_name="D", l_name="U", age=30, location="NJ", gender="M",
        language="EN", token_version=0,
    )
    db.add(u); db.commit(); db.refresh(u)
    return u


def _logged_in(db, user: User) -> None:
    """What signing in leaves behind — and what used to block every delete."""
    now = datetime.now(timezone.utc)
    db.add(RefreshToken(
        user_id=user.user_id, token_hash=uuid.uuid4().hex, family=str(uuid.uuid4()),
        expires_at=now + timedelta(days=30), created_at=now,
    ))
    db.commit()


def _exists(db, user_id: str) -> bool:
    return db.query(User).filter(User.user_id == user_id).first() is not None


def test_an_account_that_has_only_ever_logged_in_can_be_deleted():
    """The whole bug, in its smallest form."""
    db = TestingSessionLocal()
    user = _user(db, f"del_{uuid.uuid4().hex[:8]}")
    _logged_in(db, user)
    uid = user.user_id

    delete_user(user_id=uid, db=db)

    assert not _exists(db, uid)
    assert db.query(RefreshToken).filter(RefreshToken.user_id == uid).count() == 0
    db.close()


def _plan(db, client: User, *, payment_status: str = "unpaid"):
    """A client with a plan, a vendor, a booking, a message and a review."""
    uid = uuid.uuid4().hex[:8]
    vuser = _user(db, f"delv_{uid}")
    vendor = Vendor(user_id=vuser.user_id, bio="b", rating=4.5, num_events=1, category="dj")
    db.add(vendor); db.commit(); db.refresh(vendor)

    svc = Service(name="DJ", price=500.0, duration_minutes=180,
                  vendor_id=vendor.vendor_id, experience="5y", category="dj")
    db.add(svc); db.commit(); db.refresh(svc)

    now = datetime.now(timezone.utc)
    event = Event(user_id=client.user_id, name="Wedding", date_iso="2030-01-01", location="Hall")
    db.add(event); db.commit(); db.refresh(event)
    bundle = Bundle(user_id=client.user_id, name="B", event_id=event.event_id,
                    status="confirmed", created_at=now, updated_at=now)
    db.add(bundle); db.commit(); db.refresh(bundle)

    booking = Booking(
        user_id=client.user_id, vendor_id=vendor.vendor_id, service_id=svc.service_id,
        time_start="18:00", time_end="23:00", location="Hall", date_iso="2030-01-01",
        status="approved", payment_status=payment_status, amount_cents=50000,
        platform_fee_cents=2500, bundle_id=bundle.bundle_id,
    )
    db.add(booking); db.commit(); db.refresh(booking)

    db.add(Message(booking_id=booking.booking_id, sender_id=client.user_id,
                   receiver_id=vuser.user_id, content="hi", created_at=now))
    db.add(Review(booking_id=booking.booking_id, vendor_id=vendor.vendor_id,
                  service_id=svc.service_id, user_id=client.user_id,
                  rating=5.0, comment="great", created_at=now))
    db.commit()
    return vuser, vendor, svc, event, bundle, booking


def test_a_client_with_a_whole_plan_behind_them_can_be_deleted():
    db = TestingSessionLocal()
    client = _user(db, f"delc_{uuid.uuid4().hex[:8]}")
    _logged_in(db, client)
    _, _, _, event, bundle, booking = _plan(db, client)
    uid = client.user_id

    delete_user(user_id=uid, db=db)

    assert not _exists(db, uid)
    assert db.query(Booking).filter(Booking.booking_id == booking.booking_id).first() is None
    assert db.query(Bundle).filter(Bundle.bundle_id == bundle.bundle_id).first() is None
    assert db.query(Event).filter(Event.event_id == event.event_id).first() is None
    assert db.query(Message).filter(Message.sender_id == uid).count() == 0
    assert db.query(Review).filter(Review.user_id == uid).count() == 0
    db.close()


def test_a_vendor_account_takes_its_listings_with_it():
    db = TestingSessionLocal()
    client = _user(db, f"delc_{uuid.uuid4().hex[:8]}")
    vuser, vendor, svc, _, _, booking = _plan(db, client)
    db.add(VendorAvailability(vendor_id=vendor.vendor_id, day_of_week=1,
                              start_time="09:00", end_time="17:00"))
    db.commit()
    vid = vendor.vendor_id
    uid = vuser.user_id

    delete_user(user_id=uid, db=db)

    assert not _exists(db, uid)
    assert db.query(Vendor).filter(Vendor.vendor_id == vid).first() is None
    assert db.query(Service).filter(Service.service_id == svc.service_id).first() is None
    assert db.query(VendorAvailability).filter(VendorAvailability.vendor_id == vid).count() == 0
    # Their client's booking goes too — it was a booking with them.
    assert db.query(Booking).filter(Booking.booking_id == booking.booking_id).first() is None
    db.close()


def test_a_vendor_account_takes_a_booking_subject_conversation_with_it():
    """A conversation attached directly to a booking (subject_type="booking",
    e.g. a vendor enquiry that became a booking) lives outside the bundle
    cascade — _delete_bundle_cascade only ever looks up conversations by
    bundle_id. Reproduces the real bug: deleting the vendor side of a booking
    whose bundle belongs to the client used to die on
    fk_conversations_booking_id with a raw exception surfaced to the client."""
    db = TestingSessionLocal()
    client = _user(db, f"delc_{uuid.uuid4().hex[:8]}")
    vuser, vendor, svc, _, _, booking = _plan(db, client)
    now = datetime.now(timezone.utc)
    conv = Conversation(
        subject_type="booking", booking_id=booking.booking_id, vendor_id=vendor.vendor_id,
        client_user_id=client.user_id, type="direct", name="Booking chat", created_at=now,
    )
    db.add(conv); db.commit(); db.refresh(conv)
    db.add(ConversationMember(conversation_id=conv.conversation_id, user_id=client.user_id, joined_at=now))
    db.add(ConversationMember(conversation_id=conv.conversation_id, user_id=vuser.user_id, joined_at=now))
    db.add(GroupMessage(conversation_id=conv.conversation_id, sender_id=client.user_id,
                        content="hi", created_at=now))
    db.commit()
    uid = vuser.user_id
    conv_id = conv.conversation_id

    delete_user(user_id=uid, db=db)  # used to raise UserError(500, "Delete failed: ...")

    assert not _exists(db, uid)
    assert db.query(Conversation).filter(Conversation.conversation_id == conv_id).first() is None
    assert db.query(ConversationMember).filter(ConversationMember.conversation_id == conv_id).count() == 0
    assert db.query(GroupMessage).filter(GroupMessage.conversation_id == conv_id).count() == 0
    db.close()


@pytest.mark.parametrize("status", ["paid", "released", "disputed", "processing", "refunded"])
def test_money_against_a_booking_refuses_the_whole_delete(status):
    """Deleting the account wouldn't return the money, only the record of where
    it went — the same reason deleting one plan is refused."""
    db = TestingSessionLocal()
    client = _user(db, f"delm_{uuid.uuid4().hex[:8]}")
    _plan(db, client, payment_status=status)
    uid = client.user_id

    with pytest.raises(UserError) as e:
        delete_user(user_id=uid, db=db)

    assert e.value.status_code == 400
    assert "wouldn't return it" in e.value.detail
    assert _exists(db, uid), "the account must survive a refused delete"
    db.close()


def test_a_vendor_holding_a_clients_money_is_refused_too():
    """The half the client-side check can't see: a vendor closing their account
    would take bookings their clients have paid for."""
    db = TestingSessionLocal()
    client = _user(db, f"delc_{uuid.uuid4().hex[:8]}")
    vuser, _, _, _, _, _ = _plan(db, client, payment_status="paid")
    uid = vuser.user_id

    with pytest.raises(UserError) as e:
        delete_user(user_id=uid, db=db)

    assert e.value.status_code == 400
    assert _exists(db, uid)
    db.close()


def test_deleting_an_account_that_isnt_there_is_a_404():
    db = TestingSessionLocal()
    with pytest.raises(UserError) as e:
        delete_user(user_id=str(uuid.uuid4()), db=db)
    assert e.value.status_code == 404
    db.close()
