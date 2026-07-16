"""Live chat WebSocket: auth, membership, and message broadcast."""
import uuid

import pytest
from starlette.websockets import WebSocketDisconnect

from app.db.models import User
from tests.test_api import TestingSessionLocal, client, make_auth_headers
# Reuse the conversation fixture + bundle helper.
from tests.test_conversations import seeded_db, _create_bundle  # noqa: F401


def test_ws_pushes_new_message_to_a_connected_member(seeded_db):
    _bundle_id, conv_ids = _create_bundle(seeded_db)
    conv_id = conv_ids[0]
    member = seeded_db["vendor_user1"]
    sender = seeded_db["client_user"]

    with client.websocket_connect(
        f"/conversations/ws/{conv_id}", headers=make_auth_headers(member)
    ) as ws:
        resp = client.post(
            f"/conversations/{conv_id}/messages",
            json={"content": "hello live"},
            headers=make_auth_headers(sender),
        )
        assert resp.status_code == 201

        pushed = ws.receive_json()
        assert pushed["content"] == "hello live"
        assert pushed["conversation_id"] == conv_id
        assert pushed["sender_id"] == sender.user_id
        # Same shape the REST endpoint returns, so the client decodes it identically.
        assert pushed["message_id"] == resp.json()["message_id"]


def test_ws_rejects_missing_token(seeded_db):
    _bundle_id, conv_ids = _create_bundle(seeded_db)
    conv_id = conv_ids[0]
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/conversations/ws/{conv_id}") as ws:
            ws.receive_text()


def test_ws_rejects_non_member(seeded_db):
    _bundle_id, conv_ids = _create_bundle(seeded_db)
    conv_id = conv_ids[0]

    db = seeded_db["db"]
    uid = uuid.uuid4().hex[:8]
    stranger = User(
        email=f"ws_stranger_{uid}@t.com", username=f"ws_stranger_{uid}",
        password="pw", phone="1", f_name="S", l_name="T",
        age=30, location="x", gender="M", language="EN", token_version=0,
    )
    db.add(stranger); db.commit(); db.refresh(stranger)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/conversations/ws/{conv_id}", headers=make_auth_headers(stranger)
        ) as ws:
            ws.receive_text()
