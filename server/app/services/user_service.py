"""Business logic for user profile management."""

import logging

from sqlalchemy.orm import Session
from sqlalchemy import delete as sql_delete, or_

from app.db.models import (
    Booking,
    Bundle,
    ChangeRequest,
    ContentReport,
    ConversationMember,
    Event,
    GroupMessage,
    GroupMessageRead,
    Message,
    Negotiation,
    NegotiationOffer,
    PasswordResetToken,
    PushToken,
    RefreshToken,
    Review,
    Service,
    User,
    UserBlock,
    Vendor,
    VendorAvailability,
    vendor_tags,
)

logger = logging.getLogger(__name__)


class UserError(Exception):
    """Raised when a user operation fails."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _user_dict(user: User) -> dict:
    return {
        "user_id": user.user_id,
        "email": user.email,
        "username": user.username,
        "phone": user.phone,
        "f_name": user.f_name,
        "l_name": user.l_name,
        "age": user.age,
        "location": user.location,
        "gender": user.gender,
        "language": user.language,
        "pfp_url": user.pfp_url,
        # False for a Google-only account (users.password is NULL), so account
        # settings can offer "set a password" instead of asking for a current one
        # that does not exist. Never the hash itself.
        "has_password": user.password is not None,
        "open_to_price_negotiation": user.open_to_price_negotiation,
        "flexible_on_location": user.flexible_on_location,
    }


def get_user(*, user_id: str, db: Session) -> dict:
    """Return the profile for *user_id*. Raises 404 if not found."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise UserError(404, "User not found")
    return _user_dict(user)


def delete_user(*, user_id: str, db: Session) -> None:
    """Permanently delete a user and everything that hangs off them.

    This deleted bookings and the vendor row and then went straight for the
    user, which meant it never worked: fifteen-odd tables carry a foreign key to
    users.user_id, and an account that had done nothing but log in already had a
    refresh token pointing at it. Every attempt died on that constraint and
    surfaced as a 500 with the account still there — so "Delete account" was a
    button that could not succeed for anybody.

    Order is the whole job. Children before parents, and the two existing
    cascades do most of it: _delete_bundle_cascade takes a plan's bookings,
    conversations and event, and _delete_booking_cascade takes a booking's
    negotiations, messages and reviews. Reusing them keeps this path honest with
    the ones clients already use, including their refusal when money has moved.
    """
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise UserError(404, "User not found")

    from app.services.bundle_service import (
        BundleError,
        _delete_booking_cascade,
        _delete_bundle_cascade,
        _money_has_moved,
    )

    vendor = db.query(Vendor).filter(Vendor.user_id == user_id).first()
    vendor_id = vendor.vendor_id if vendor else None

    # Both sides. A vendor's account holds their clients' bookings as surely as
    # a client's holds their own, and deleting it takes those with it.
    sides = [Booking.user_id == user_id]
    if vendor_id:
        sides.append(Booking.vendor_id == vendor_id)

    # Asked before anything is touched, the way delete_bundle asks. The cascade
    # refuses these too, but only once it is halfway through a transaction —
    # this is the version that can name the problem.
    held = [b for b in db.query(Booking).filter(or_(*sides)).all() if _money_has_moved(b)]
    if held:
        raise UserError(
            400,
            f"{len(held)} of your bookings {'has' if len(held) == 1 else 'have'} money "
            "against them. Deleting your account wouldn't return it — it would only "
            "lose the record of where it went. Refund or resolve "
            f"{'that booking' if len(held) == 1 else 'those bookings'} first, then "
            "delete your account.",
        )

    service_ids = (
        [s.service_id for s in db.query(Service).filter(Service.vendor_id == vendor_id).all()]
        if vendor_id
        else []
    )

    try:
        # Their plans, with the bookings and conversations hanging off each.
        for bundle in db.query(Bundle).filter(Bundle.user_id == user_id).all():
            _delete_bundle_cascade(bundle, db)
        db.flush()

        # Whatever the plans didn't cover: bookings they take as a vendor, and
        # any that never belonged to a bundle.
        for booking in db.query(Booking).filter(or_(*sides)).all():
            _delete_booking_cascade(booking, db)
        db.flush()

        # Rows that point at the person rather than at a plan. Group messages
        # before the reads that reference them; offers before negotiations.
        own_group_messages = [
            m.message_id
            for m in db.query(GroupMessage).filter(GroupMessage.sender_id == user_id).all()
        ]
        if own_group_messages:
            db.query(GroupMessageRead).filter(
                GroupMessageRead.message_id.in_(own_group_messages)
            ).delete(synchronize_session=False)
        db.query(GroupMessageRead).filter(GroupMessageRead.user_id == user_id).delete(
            synchronize_session=False
        )
        db.query(GroupMessage).filter(GroupMessage.sender_id == user_id).delete(
            synchronize_session=False
        )
        db.query(ConversationMember).filter(ConversationMember.user_id == user_id).delete(
            synchronize_session=False
        )
        db.query(Message).filter(
            or_(Message.sender_id == user_id, Message.receiver_id == user_id)
        ).delete(synchronize_session=False)

        own_negotiations = [
            n.negotiation_id
            for n in db.query(Negotiation).filter(Negotiation.proposed_by == user_id).all()
        ]
        db.query(NegotiationOffer).filter(NegotiationOffer.proposed_by == user_id).delete(
            synchronize_session=False
        )
        if own_negotiations:
            db.query(NegotiationOffer).filter(
                NegotiationOffer.negotiation_id.in_(own_negotiations)
            ).delete(synchronize_session=False)
            db.query(Negotiation).filter(
                Negotiation.negotiation_id.in_(own_negotiations)
            ).delete(synchronize_session=False)
        db.query(ChangeRequest).filter(ChangeRequest.proposed_by == user_id).delete(
            synchronize_session=False
        )

        # Reviews they wrote, and — for a vendor — the ones written about them.
        # Both before services, which reviews reference.
        review_sides = [Review.user_id == user_id]
        if vendor_id:
            review_sides.append(Review.vendor_id == vendor_id)
        if service_ids:
            review_sides.append(Review.service_id.in_(service_ids))
        db.query(Review).filter(or_(*review_sides)).delete(synchronize_session=False)

        db.query(Event).filter(Event.user_id == user_id).delete(synchronize_session=False)
        db.query(PushToken).filter(PushToken.user_id == user_id).delete(synchronize_session=False)
        db.query(RefreshToken).filter(RefreshToken.user_id == user_id).delete(
            synchronize_session=False
        )
        db.query(PasswordResetToken).filter(PasswordResetToken.user_id == user_id).delete(
            synchronize_session=False
        )
        db.query(ContentReport).filter(ContentReport.reporter_user_id == user_id).delete(
            synchronize_session=False
        )
        # Both directions: blocks they made, and blocks made against them.
        db.query(UserBlock).filter(
            or_(UserBlock.blocker_user_id == user_id, UserBlock.blocked_user_id == user_id)
        ).delete(synchronize_session=False)
        db.flush()

        if vendor_id:
            db.execute(
                sql_delete(VendorAvailability).where(VendorAvailability.vendor_id == vendor_id)
            )
            db.execute(vendor_tags.delete().where(vendor_tags.c.vendor_id == vendor_id))
            db.execute(sql_delete(Service).where(Service.vendor_id == vendor_id))
            db.flush()
            db.delete(vendor)
            db.flush()

        db.delete(user)
        db.commit()
    except BundleError as e:
        # A considered refusal from the cascade — money moved between the check
        # above and here. Pass it on as one rather than as a 500.
        db.rollback()
        raise UserError(e.status_code, e.detail)
    except Exception as exc:
        db.rollback()
        logger.exception("delete_user failed for user %s", user_id)
        raise UserError(500, f"Delete failed: {exc}")


def update_user(*, user_id: str, update_data: dict, db: Session) -> dict:
    """Apply *update_data* (partial) to the user and return the updated profile."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise UserError(404, "User not found")
    if "email" in update_data:
        conflict = db.query(User).filter(
            User.email == update_data["email"],
            User.user_id != user_id,
        ).first()
        if conflict:
            raise UserError(400, "Email is already in use by another account")
    for field, value in update_data.items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return _user_dict(user)
