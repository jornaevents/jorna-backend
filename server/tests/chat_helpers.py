"""Contracts go out in the client's chat (docs/DECISIONS.md #48), so a sent
contract needs a client account with a thread. Most tests are about the
contract itself, not who it's for; `install` makes POST /contracts and
POST /leads/{id}/convert start a chat with a fresh client when a test sends
one without naming a client. A test that names `conversation_id` (even as
null, to check the refusal) is left alone."""

import re
import uuid
from datetime import datetime, timezone

from app.db.models import User

_CONVERT = re.compile(r"^/leads/[^/]+/convert$")


def chat_with(client, session_factory, make_headers, vendor_headers) -> str:
    """A new client account asks this vendor a question; returns the thread."""
    vendor = client.get("/vendors/me", headers=vendor_headers).json()
    uid = uuid.uuid4().hex[:8]
    db = session_factory()
    user = User(
        email=f"chat_client_{uid}@test.com", username=f"chat_client_{uid}", password="pw",
        f_name="Chat", l_name="Client", token_version=0,
        email_verified_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(user); db.commit(); db.refresh(user)
    headers = make_headers(user)
    db.close()
    resp = client.post(
        "/conversations/enquiry", json={"vendor_id": vendor["vendor_id"], "content": "Hello!"}, headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["conversation_id"]


_installed: dict = {}


def new_chat(vendor_headers) -> str:
    """A thread between this vendor and a fresh client account."""
    return chat_with(_installed["client"], _installed["sessions"], _installed["headers"], vendor_headers)


def as_link_contract(booking_id: str, session_factory, *, email: str | None = "keep") -> None:
    """Make a contract look like one sent by link before #48: no client
    account (and, with email=None, no email on file)."""
    from app.db.models import Booking

    db = session_factory()
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).one()
    booking.user_id = None
    if email != "keep":
        booking.guest_email = email
    db.commit()
    db.close()


def install(client, session_factory, make_headers) -> None:
    _installed.update(client=client, sessions=session_factory, headers=make_headers)
    original = client.post

    def post(url, *args, json=None, headers=None, **kwargs):
        sends = (
            isinstance(json, dict)
            and (url == "/contracts" or _CONVERT.match(url))
            and not json.get("draft")
            and "conversation_id" not in json
            and headers
        )
        if sends:
            json = {**json, "conversation_id": chat_with(client, session_factory, make_headers, headers)}
        return original(url, *args, json=json, headers=headers, **kwargs)

    client.post = post
