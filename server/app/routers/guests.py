"""Guest lists and RSVPs.

Two audiences in one file, and the split matters: everything under /events is
the host's and requires their account, everything under /rsvp is a guest's and
is authenticated by the link they were sent. A token is the whole credential
there, so those responses carry the invitation and nothing else — no guest list,
no other guests, nothing about the host's vendors or what they've paid.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.dependencies import get_current_user
from app.limiter import limiter
from app.services.guest_service import (
    GuestError,
    add_function,
    add_guest,
    add_guests_bulk,
    delete_function,
    delete_guest,
    invitation_for_guest,
    invitation_for_open_link,
    join_via_open_link,
    list_guests,
    open_link_token,
    record_reply,
    retire_open_link,
    update_function,
    update_guest,
)

router = APIRouter(tags=["guests"])


def _fail(e: GuestError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=e.detail)


# ── The host's list ───────────────────────────────────────────────────


class FunctionIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    date_iso: Optional[str] = None
    time_start: Optional[str] = None
    time_end: Optional[str] = None
    location: Optional[str] = None


class FunctionPatch(BaseModel):
    name: Optional[str] = None
    date_iso: Optional[str] = None
    time_start: Optional[str] = None
    time_end: Optional[str] = None
    location: Optional[str] = None


class GuestIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    email: Optional[str] = None
    phone: Optional[str] = None
    party_size: int = Field(1, ge=1, le=99)
    # Omitted means every function — what a host means by adding somebody to the
    # guest list without saying which parts.
    function_ids: list[str] = Field(default_factory=list)
    note: Optional[str] = None


class GuestPatch(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    party_size: Optional[int] = Field(None, ge=1, le=99)
    function_ids: Optional[list[str]] = None
    note: Optional[str] = None


class BulkGuestsIn(BaseModel):
    """Pasted lines: a name, optionally an email and a party size, comma or tab
    separated — "Anita Sharma, anita@example.com, 3"."""
    lines: list[str] = Field(..., max_length=500)
    function_ids: list[str] = Field(default_factory=list)


@router.get("/events/{event_id}/guests", summary="The guest list, its functions, and the headcount")
def guests_for_event(
    event_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return list_guests(event_id=event_id, user_id=current_user.user_id, db=db)
    except GuestError as e:
        raise _fail(e)


@router.post("/events/{event_id}/guests", summary="Add a guest", status_code=201)
def create_guest(
    event_id: str,
    body: GuestIn,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return add_guest(
            event_id=event_id, user_id=current_user.user_id, name=body.name,
            email=body.email, phone=body.phone, party_size=body.party_size,
            function_ids=body.function_ids, note=body.note, db=db,
        )
    except GuestError as e:
        raise _fail(e)


@router.post("/events/{event_id}/guests/bulk", summary="Add many guests from pasted lines")
def create_guests_bulk(
    event_id: str,
    body: BulkGuestsIn,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return add_guests_bulk(
            event_id=event_id, user_id=current_user.user_id,
            lines=body.lines, function_ids=body.function_ids, db=db,
        )
    except GuestError as e:
        raise _fail(e)


@router.patch("/guests/{guest_id}", summary="Edit a guest")
def edit_guest(
    guest_id: str,
    body: GuestPatch,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return update_guest(
            guest_id=guest_id, user_id=current_user.user_id,
            updates=body.model_dump(exclude_unset=True), db=db,
        )
    except GuestError as e:
        raise _fail(e)


@router.delete("/guests/{guest_id}", summary="Remove a guest", status_code=204)
def remove_guest(
    guest_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        delete_guest(guest_id=guest_id, user_id=current_user.user_id, db=db)
    except GuestError as e:
        raise _fail(e)


# ── Functions ─────────────────────────────────────────────────────────


@router.post("/events/{event_id}/functions", summary="Add a function", status_code=201)
def create_function(
    event_id: str,
    body: FunctionIn,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return add_function(
            event_id=event_id, user_id=current_user.user_id, name=body.name,
            date_iso=body.date_iso, time_start=body.time_start,
            time_end=body.time_end, location=body.location, db=db,
        )
    except GuestError as e:
        raise _fail(e)


@router.patch("/functions/{function_id}", summary="Edit a function")
def edit_function(
    function_id: str,
    body: FunctionPatch,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return update_function(
            function_id=function_id, user_id=current_user.user_id,
            updates=body.model_dump(exclude_unset=True), db=db,
        )
    except GuestError as e:
        raise _fail(e)


@router.delete("/functions/{function_id}", summary="Remove a function", status_code=204)
def remove_function(
    function_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        delete_function(function_id=function_id, user_id=current_user.user_id, db=db)
    except GuestError as e:
        raise _fail(e)


# ── The open link ─────────────────────────────────────────────────────


@router.post("/events/{event_id}/invite-link", summary="Get (or mint) the shareable link")
def create_open_link(
    event_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """One link for a family group chat, where whoever opens it adds themselves.

    Minted only when asked for, because anybody holding it can add to a headcount
    that costs money.
    """
    try:
        return open_link_token(event_id=event_id, user_id=current_user.user_id, db=db)
    except GuestError as e:
        raise _fail(e)


@router.delete("/events/{event_id}/invite-link", summary="Stop the shareable link working")
def revoke_open_link(
    event_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return retire_open_link(event_id=event_id, user_id=current_user.user_id, db=db)
    except GuestError as e:
        raise _fail(e)


# ── The guest's side ──────────────────────────────────────────────────
#
# No account. The token in the link is the credential, which is why these are
# rate limited and return only what an invitation should say.


class ReplyIn(BaseModel):
    function_id: str
    status: str = Field(..., pattern="^(attending|declined)$")
    attending_count: Optional[int] = Field(None, ge=0, le=99)


class RepliesIn(BaseModel):
    replies: list[ReplyIn] = Field(default_factory=list)


class JoinIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    email: Optional[str] = None
    party_size: int = Field(1, ge=1, le=99)
    replies: list[ReplyIn] = Field(default_factory=list)


@router.get("/rsvp/{token}", summary="The invitation this link opens")
@limiter.limit("60/minute")
def read_invitation(request: Request, token: str, db: Session = Depends(get_db)):
    """Reads only.

    Deliberately: link previews and mail scanners fetch URLs before a person
    sees them, so nothing that changes an answer can live on a GET.
    """
    try:
        return invitation_for_guest(token=token, db=db)
    except GuestError:
        # Might be the shared link rather than somebody's own.
        try:
            return invitation_for_open_link(token=token, db=db)
        except GuestError as e:
            raise _fail(e)


@router.post("/rsvp/{token}", summary="Answer an invitation")
@limiter.limit("30/minute")
def send_reply(
    request: Request,
    token: str,
    body: RepliesIn,
    db: Session = Depends(get_db),
):
    try:
        return record_reply(
            token=token, replies=[r.model_dump() for r in body.replies], db=db
        )
    except GuestError as e:
        raise _fail(e)


@router.post("/rsvp/{token}/join", summary="Add yourself from a shared link")
@limiter.limit("20/minute")
def join_open(
    request: Request,
    token: str,
    body: JoinIn,
    db: Session = Depends(get_db),
):
    """For the link a host drops in a group chat. Returns a token of their own,
    so from here they hold an ordinary invitation they can come back and change."""
    try:
        return join_via_open_link(
            token=token, name=body.name, email=body.email,
            party_size=body.party_size,
            replies=[r.model_dump() for r in body.replies], db=db,
        )
    except GuestError as e:
        raise _fail(e)
