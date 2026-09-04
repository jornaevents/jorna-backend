"""Moving a booking that has already been agreed.

Once a request reaches a vendor, plan_readiness.COMMITTED_FIELDS freezes what
they were told. That is right — it is what they agreed to — but it left a paid
booking with no route to a new date at all, and events move. The only exits were
a dispute, which is adversarial and the wrong shape for "the venue flooded", or
nothing, which leaves a vendor turning up on a dead date and escrow releasing a
week after a day that stopped meaning anything.

The shape is a negotiation's: turn-based, the server decides whose turn it is,
and a resolution mutates the booking. One mental model for "I want to change
something we agreed", whether the thing is a price or a date.

Two properties hold throughout:

  Escrow does not move on a proposal, only on a resolution. A client cannot
  free their money by proposing an impossible date; a vendor cannot strand it
  by ignoring one.

  A proposal is plan-wide, an answer is per vendor. A wedding moving takes
  every vendor with it, so the client proposes once — but one vendor being
  unable to come should not veto a move the client still wants.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.db.models import Booking, Bundle, ChangeRequest, Service, Vendor
from app.models.schemas import BookingStatus

logger = logging.getLogger(__name__)


class ChangeRequestError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# How long a vendor may sit on a request before the client can act without them.
#
# The same number auto_release_due uses for the other place this product decides
# somebody has gone quiet. One waiting period rather than two — if that figure
# ever moves, both should move together, which is why this reads it rather than
# restating it.
from app.services.stripe_service import AUTO_RELEASE_DAYS  # noqa: E402

RESPONSE_WINDOW_DAYS = AUTO_RELEASE_DAYS

# A request that is still somebody's turn.
OPEN = "pending"
# Resolutions. `expired` is the sweep's; the rest are somebody's decision.
ACCEPTED, DECLINED, WITHDRAWN, EXPIRED = "accepted", "declined", "withdrawn", "expired"

# Bookings a proposal is worth sending about: alive, and actually committed to
# something. A pending request is still editable by the ordinary route, and a
# dead one is not going to happen whatever date it names.
_MOVABLE_STATUSES = (
    BookingStatus.APPROVED.value,
    BookingStatus.PAYMENT_CONFIRMED.value,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """Stored naive-UTC in some columns; compared against an aware now()."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _dict(cr: ChangeRequest, booking: Booking | None = None, service: Service | None = None) -> dict:
    """One request, as the clients read it."""
    out = {
        "change_request_id": cr.change_request_id,
        "booking_id": cr.booking_id,
        "status": cr.status,
        "proposed_by": cr.proposed_by,
        "date_iso": cr.date_iso,
        "date_end": cr.date_end,
        "time_start": cr.time_start,
        "time_end": cr.time_end,
        "message": cr.message,
        "response_message": cr.response_message,
        # Present only while a price rise is waiting on the client. Its presence
        # IS the "needs your consent" state — there is no separate flag to get
        # out of step with it.
        "repriced_amount_cents": cr.repriced_amount_cents,
        "client_consented_at": (
            cr.client_consented_at.isoformat() if cr.client_consented_at else None
        ),
        "created_at": cr.created_at.isoformat() if cr.created_at else None,
        "resolved_at": cr.resolved_at.isoformat() if cr.resolved_at else None,
        "expires_at": (
            (_aware(cr.created_at) + timedelta(days=RESPONSE_WINDOW_DAYS)).isoformat()
            if cr.created_at and cr.status == OPEN
            else None
        ),
    }
    if booking is not None:
        out["service_name"] = service.name if service else None
        out["vendor_id"] = booking.vendor_id
        # What it is moving *from*, so a client reading the board doesn't have to
        # hold the old dates in their head.
        out["from_date_iso"] = booking.date_iso
        out["from_date_end"] = booking.date_end
        out["from_time_start"] = booking.time_start
        out["from_time_end"] = booking.time_end
    return out


def _proposed_fields(cr: ChangeRequest, booking: Booking) -> dict:
    """The booking's dates as they would be if this request were accepted.

    Null on the request means "unchanged", so this is the merge — used both to
    re-price and to check the vendor's other bookings, which must agree about
    what is being asked for.
    """
    return {
        "date_iso": cr.date_iso or booking.date_iso,
        "date_end": cr.date_end if cr.date_end is not None else booking.date_end,
        "time_start": cr.time_start or booking.time_start,
        "time_end": cr.time_end or booking.time_end,
    }


def _expire_stale(db: Session, requests: list[ChangeRequest]) -> None:
    """Mark anything past its window, so a read never reports a live request
    that is actually out of time. The sweep below does the same for everything;
    this keeps a single plan honest without waiting for it."""
    cutoff = _now() - timedelta(days=RESPONSE_WINDOW_DAYS)
    changed = False
    for cr in requests:
        if cr.status == OPEN and _aware(cr.created_at) and _aware(cr.created_at) < cutoff:
            cr.status = EXPIRED
            cr.resolved_at = _now()
            changed = True
    if changed:
        db.commit()


# ── Propose ───────────────────────────────────────────────────────────


def propose(
    *,
    bundle_id: str,
    caller_user_id: str,
    date_iso: str | None,
    date_end: str | None,
    time_start: str | None,
    time_end: str | None,
    message: str | None,
    db: Session,
) -> dict:
    """Ask every vendor on a plan to move.

    One action, n rows. The bookings already share a bundle, so nothing new owns
    the grouping — the board is read back by joining on it.
    """
    if not any((date_iso, date_end, time_start, time_end)):
        raise ChangeRequestError(400, "Say what you'd like to move it to.")

    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise ChangeRequestError(404, "Plan not found")
    if bundle.user_id != caller_user_id:
        raise ChangeRequestError(403, "You do not own this plan")
    if bundle.status == "draft":
        # A draft's details are still directly editable and no vendor has been
        # told anything, so asking them to agree to a change is asking about a
        # conversation that hasn't happened.
        raise ChangeRequestError(
            400,
            "This plan hasn't been sent yet, so its date is still yours to edit "
            "directly.",
        )

    bookings = (
        db.query(Booking)
        .filter(
            Booking.bundle_id == bundle_id,
            Booking.status.in_(_MOVABLE_STATUSES),
        )
        .all()
    )
    if not bookings:
        raise ChangeRequestError(
            400,
            "Nothing on this plan has been agreed yet — there's no one to ask.",
        )

    # One open proposal at a time. Two live sets of proposed dates on one plan
    # is a question with two answers, and whichever vendor replies last would
    # silently win.
    existing = (
        db.query(ChangeRequest)
        .filter(
            ChangeRequest.booking_id.in_([b.booking_id for b in bookings]),
            ChangeRequest.status == OPEN,
        )
        .all()
    )
    _expire_stale(db, existing)
    if any(cr.status == OPEN for cr in existing):
        raise ChangeRequestError(
            409,
            "There's already a date change out with your vendors. Withdraw it "
            "before proposing another.",
        )

    now = _now()
    created: list[ChangeRequest] = []
    for booking in bookings:
        cr = ChangeRequest(
            booking_id=booking.booking_id,
            proposed_by=caller_user_id,
            status=OPEN,
            date_iso=date_iso,
            date_end=date_end,
            time_start=time_start,
            time_end=time_end,
            message=message or None,
            created_at=now,
        )
        db.add(cr)
        created.append(cr)
    db.commit()
    for cr in created:
        db.refresh(cr)

    _notify(created, db, kind="proposed")
    for cr in created:
        _post_reschedule_message(
            cr, action="proposed", sender_user_id=caller_user_id, db=db, message=message
        )
    return {"requests": [_dict(cr) for cr in created], "count": len(created)}


# ── Respond ───────────────────────────────────────────────────────────


def respond(
    *,
    change_request_id: str,
    caller_user_id: str,
    accept: bool,
    message: str | None,
    db: Session,
) -> dict:
    """The vendor's answer.

    Accepting is the same commitment as approving a booking in the first place,
    so it meets the same bar: the vendor's other work is re-checked, and a clash
    refuses rather than double-books them. Nothing else in the app lets a vendor
    be in two places, and this must not be the exception.
    """
    cr = (
        db.query(ChangeRequest)
        .filter(ChangeRequest.change_request_id == change_request_id)
        .first()
    )
    if not cr:
        raise ChangeRequestError(404, "Change request not found")

    booking = db.query(Booking).filter(Booking.booking_id == cr.booking_id).first()
    if not booking:
        raise ChangeRequestError(404, "Booking not found")

    vendor = db.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    if not vendor or vendor.user_id != caller_user_id:
        raise ChangeRequestError(403, "Only this booking's vendor can answer")

    _expire_stale(db, [cr])
    if cr.status != OPEN:
        raise ChangeRequestError(400, f"This request is already {cr.status}.")

    if not accept:
        cr.status = DECLINED
        cr.response_message = (message or None)
        cr.resolved_at = _now()
        db.commit()
        _notify([cr], db, kind="declined")
        _post_reschedule_message(
            cr, action="declined", sender_user_id=caller_user_id, db=db, message=message
        )
        return _dict(cr, booking)

    # The same guard approval uses. Excluding this booking, because its own
    # current dates are not a conflict with its proposed ones.
    from app.services.booking_service import vendor_has_conflicting_booking

    fields = _proposed_fields(cr, booking)
    clash = vendor_has_conflicting_booking(
        vendor_id=booking.vendor_id,
        date_iso=fields["date_iso"],
        date_end=fields["date_end"],
        time_start=fields["time_start"],
        time_end=fields["time_end"],
        db=db,
        exclude_booking_id=booking.booking_id,
    )
    if clash:
        # Named, so the vendor knows why without going to look. They decline
        # instead — the request stays open, which is theirs to resolve.
        when = clash.date_iso or "that date"
        raise ChangeRequestError(
            409,
            f"You already have a booking on {when}. Decline this request if you "
            "can't make the new date.",
        )

    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    old_cents = booking.amount_cents
    new_cents = _reprice(booking, service, fields)

    # A rise waits for the client. Nobody should be charged more by a flow they
    # started to solve a scheduling problem — and the card on file means an
    # unguarded rise would charge itself.
    if (
        new_cents is not None
        and old_cents is not None
        and new_cents > old_cents
        and cr.client_consented_at is None
    ):
        cr.repriced_amount_cents = new_cents
        cr.response_message = message or None
        db.commit()
        _notify([cr], db, kind="needs_consent")
        _post_reschedule_message(
            cr, action="needs_consent", sender_user_id=caller_user_id, db=db, message=message
        )
        return _dict(cr, booking, service)

    _apply(cr, booking, service, fields, new_cents, db)
    cr.response_message = message or None
    db.commit()
    _notify([cr], db, kind="accepted")
    _post_reschedule_message(
        cr, action="accepted", sender_user_id=caller_user_id, db=db, message=message
    )
    return _dict(cr, booking, service)


def _reprice(booking: Booking, service: Service | None, fields: dict) -> int | None:
    """What the booking would cost on the proposed dates.

    None when it can't be worked out — a per-person service with no headcount,
    say — in which case the move goes through and the price stays pending
    exactly as it already was.
    """
    from app.services.booking_service import estimate_amount_cents

    return estimate_amount_cents(
        service,
        guest_count=booking.guest_count,
        performer_count=booking.performer_count,
        date_iso=fields["date_iso"],
        date_end=fields["date_end"],
        time_start=fields["time_start"],
        time_end=fields["time_end"],
    )


def _apply(
    cr: ChangeRequest,
    booking: Booking,
    service: Service | None,
    fields: dict,
    new_cents: int | None,
    db: Session,
) -> None:
    """Move the booking. The only place a change request writes to one."""
    booking.date_iso = fields["date_iso"]
    booking.date_end = fields["date_end"]
    booking.time_start = fields["time_start"]
    booking.time_end = fields["time_end"]

    old_cents = booking.amount_cents
    if new_cents is not None:
        booking.amount_cents = new_cents
        # A cheaper booking returns the difference. Doing it here rather than
        # asking the client to notice and request it — they did not ask to be
        # holding a credit.
        if old_cents is not None and new_cents < old_cents and booking.payment_intent_id:
            _refund_difference(booking, old_cents - new_cents)

    cr.status = ACCEPTED
    cr.resolved_at = _now()

    # The plan's date is what the run sheet and the escrow gate read, so it
    # follows the bookings it was derived from.
    try:
        from app.services.booking_service import sync_event_venue

        sync_event_venue(booking.bundle_id, db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("change request: venue re-sync failed: %s", exc)

    # The booking already has a Google event (it was approved to get a
    # change request in the first place) — this becomes an update, same
    # best-effort contract, own internal try/except.
    from app.services.calendar_service import sync_booking_to_calendar

    sync_booking_to_calendar(booking, db)


def _refund_difference(booking: Booking, amount_cents: int) -> None:
    """Return what a shortened booking no longer costs.

    Failure is logged rather than raised: the move itself is agreed and should
    not be undone because a refund needs retrying. The money is still visible on
    the booking, and support can settle it.
    """
    if amount_cents <= 0:
        return
    try:
        import stripe

        stripe.Refund.create(
            payment_intent=booking.payment_intent_id,
            amount=amount_cents,
            reason="requested_by_customer",
        )
        logger.info(
            "Refunded %s cents on booking %s after a shortening reschedule",
            amount_cents,
            booking.booking_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Reschedule refund failed for booking %s (%s cents): %s",
            booking.booking_id,
            amount_cents,
            exc,
        )


# ── Consent to a price rise ───────────────────────────────────────────


def consent(*, change_request_id: str, caller_user_id: str, db: Session) -> dict:
    """The client agreeing to pay more for the new dates.

    The vendor has already accepted; this is the second half. Only the
    difference is charged — the original payment stands.
    """
    cr = (
        db.query(ChangeRequest)
        .filter(ChangeRequest.change_request_id == change_request_id)
        .first()
    )
    if not cr:
        raise ChangeRequestError(404, "Change request not found")
    booking = db.query(Booking).filter(Booking.booking_id == cr.booking_id).first()
    if not booking:
        raise ChangeRequestError(404, "Booking not found")
    if booking.user_id != caller_user_id:
        raise ChangeRequestError(403, "You are not the customer for this booking")
    if cr.status != OPEN or cr.repriced_amount_cents is None:
        raise ChangeRequestError(400, "This request isn't waiting on your approval.")

    service = db.query(Service).filter(Service.service_id == booking.service_id).first()
    fields = _proposed_fields(cr, booking)
    difference = cr.repriced_amount_cents - (booking.amount_cents or 0)

    cr.client_consented_at = _now()
    _apply(cr, booking, service, fields, cr.repriced_amount_cents, db)
    db.commit()

    _notify([cr], db, kind="accepted")
    _post_reschedule_message(cr, action="consented", sender_user_id=caller_user_id, db=db)
    return {
        **_dict(cr, booking, service),
        "additional_cents": max(difference, 0),
        "message": (
            f"Moved. The extra ${difference / 100:,.2f} will be charged to your "
            "card on file."
            if difference > 0
            else "Moved."
        ),
    }


# ── Withdraw ──────────────────────────────────────────────────────────


def withdraw(*, bundle_id: str, caller_user_id: str, db: Session) -> dict:
    """Take back every outstanding request on a plan."""
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise ChangeRequestError(404, "Plan not found")
    if bundle.user_id != caller_user_id:
        raise ChangeRequestError(403, "You do not own this plan")

    booking_ids = [
        b.booking_id
        for b in db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    ]
    open_requests = (
        db.query(ChangeRequest)
        .filter(
            ChangeRequest.booking_id.in_(booking_ids or [""]),
            ChangeRequest.status == OPEN,
        )
        .all()
    )
    now = _now()
    for cr in open_requests:
        cr.status = WITHDRAWN
        cr.resolved_at = now
    db.commit()
    for cr in open_requests:
        _post_reschedule_message(cr, action="withdrawn", sender_user_id=caller_user_id, db=db)
    return {"withdrawn": len(open_requests)}


# ── Read ──────────────────────────────────────────────────────────────


def for_bundle(*, bundle_id: str, caller_user_id: str, db: Session) -> dict:
    """The board: every vendor's answer to the current proposal."""
    bundle = db.query(Bundle).filter(Bundle.bundle_id == bundle_id).first()
    if not bundle:
        raise ChangeRequestError(404, "Plan not found")
    if bundle.user_id != caller_user_id:
        raise ChangeRequestError(403, "You do not own this plan")

    bookings = {
        b.booking_id: b
        for b in db.query(Booking).filter(Booking.bundle_id == bundle_id).all()
    }
    if not bookings:
        return {"requests": []}

    requests = (
        db.query(ChangeRequest)
        .filter(ChangeRequest.booking_id.in_(list(bookings)))
        .order_by(ChangeRequest.created_at.desc())
        .all()
    )
    _expire_stale(db, requests)

    # Only the latest round. Older ones are history, and a board showing three
    # months of superseded proposals answers nothing.
    if not requests:
        return {"requests": []}
    latest = max(_aware(r.created_at) for r in requests if r.created_at)
    current = [r for r in requests if _aware(r.created_at) == latest]

    services = {
        s.service_id: s
        for s in db.query(Service)
        .filter(Service.service_id.in_([b.service_id for b in bookings.values()]))
        .all()
    }
    return {
        "requests": [
            _dict(
                cr,
                bookings[cr.booking_id],
                services.get(bookings[cr.booking_id].service_id),
            )
            for cr in current
        ]
    }


def open_for_booking(booking_id: str, db: Session) -> ChangeRequest | None:
    """The request a vendor still owes an answer on, if there is one."""
    cr = (
        db.query(ChangeRequest)
        .filter(
            ChangeRequest.booking_id == booking_id,
            ChangeRequest.status == OPEN,
        )
        .order_by(ChangeRequest.created_at.desc())
        .first()
    )
    if cr:
        _expire_stale(db, [cr])
        if cr.status != OPEN:
            return None
    return cr


# ── The sweep ─────────────────────────────────────────────────────────


def expire_due(*, db: Session, now: datetime | None = None) -> dict:
    """Close out requests no vendor answered in time.

    Silence is not agreement here — the opposite of auto_release_due, where it
    is. The difference is what the silence would cost: there, the vendor has
    done the work and is owed; here, nobody has agreed to be anywhere on a new
    date, so treating it as a yes would send a client to a venue expecting
    vendors who never said they'd come.
    """
    now = now or _now()
    cutoff = now - timedelta(days=RESPONSE_WINDOW_DAYS)
    stale = (
        db.query(ChangeRequest)
        .filter(ChangeRequest.status == OPEN, ChangeRequest.created_at < cutoff)
        .all()
    )
    for cr in stale:
        cr.status = EXPIRED
        cr.resolved_at = now
    if stale:
        db.commit()
    # Attributed to whoever proposed it — always the client in v1 (see
    # ChangeRequest.proposed_by) — worded impersonally in _reschedule_line
    # so it doesn't read as them announcing their own request's expiry.
    for cr in stale:
        _post_reschedule_message(cr, action="expired", sender_user_id=cr.proposed_by, db=db)
    return {"expired": [cr.change_request_id for cr in stale]}


# ── Notifications ─────────────────────────────────────────────────────


def _reschedule_line(action: str, message: str | None = None) -> str:
    """The sentence a system message carries into the booking's thread.

    Same convention as negotiation_service._offer_line: a short lead, plus
    whoever acted's own note when there is one — so a thread reader gets
    the same "why", not just the "what", as the push notification does.
    """
    lead = {
        "proposed": "Proposed a new date for this booking",
        "accepted": "Accepted the new date",
        "declined": "Declined the reschedule",
        "needs_consent": "The new date costs more — waiting on the client to approve",
        "consented": "Approved the new price. The move is final",
        "withdrawn": "Withdrew the reschedule request",
        "expired": "The reschedule request expired without a response",
    }.get(action, action)
    return f"{lead} — {message.strip()}" if message and message.strip() else lead


def _post_reschedule_message(
    cr: "ChangeRequest", *, action: str, sender_user_id: str, db: Session,
    message: str | None = None,
) -> None:
    """Write this reschedule event into the booking's own thread.

    Best-effort — a failure here must never surface as a failure of the
    reschedule action itself. post_system_message already swallows its own
    errors, but this wraps the call too: _reschedule_line or the import
    itself failing must not propagate any more than a failure inside
    post_system_message would. Local import: conversation_service pulls in
    more of the app than this module needs at load time, same reasoning as
    the other cross-service imports below.
    """
    try:
        from app.services.conversation_service import post_system_message

        post_system_message(
            booking_id=cr.booking_id,
            sender_user_id=sender_user_id,
            content=_reschedule_line(action, message),
            meta={"change_request_id": cr.change_request_id, "action": action},
            db=db,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Couldn't post reschedule message for booking %s (%s): %s",
            cr.booking_id, action, exc,
        )


def _notify(requests: list[ChangeRequest], db: Session, *, kind: str) -> None:
    """Tell whoever's turn it now is.

    Best-effort, like every other notification path here: a change request that
    is recorded but unannounced is recoverable, and one that fails to record
    because an email bounced is not.
    """
    try:
        from app.utils.notifications import send_push_to_user
        from app.db.models import User

        for cr in requests:
            booking = (
                db.query(Booking).filter(Booking.booking_id == cr.booking_id).first()
            )
            if not booking:
                continue
            service = (
                db.query(Service)
                .filter(Service.service_id == booking.service_id)
                .first()
            )
            name = service.name if service else "a booking"

            if kind == "proposed":
                vendor = (
                    db.query(Vendor)
                    .filter(Vendor.vendor_id == booking.vendor_id)
                    .first()
                )
                target = (
                    db.query(User).filter(User.user_id == vendor.user_id).first()
                    if vendor
                    else None
                )
                title = "A client wants to move a booking"
                body = f"{name} — they've proposed a new date. You have {RESPONSE_WINDOW_DAYS} days to answer."
            else:
                target = db.query(User).filter(User.user_id == booking.user_id).first()
                title = {
                    "accepted": "Your vendor accepted the new date",
                    "declined": "Your vendor can't make the new date",
                    "needs_consent": "The new date costs more",
                }.get(kind, "Your date change was answered")
                body = {
                    "accepted": f"{name} has moved.",
                    "declined": f"{name} stays as booked — you can keep it or ask for a refund.",
                    "needs_consent": f"{name} costs more on the new dates. Approve to go ahead.",
                }.get(kind, name)

            if target:
                send_push_to_user(
                    target,
                    title,
                    body,
                    {
                        "booking_id": booking.booking_id,
                        "change_request_id": cr.change_request_id,
                        "type": "change_request",
                    },
                    db=db,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("change request notification failed (%s): %s", kind, exc)
