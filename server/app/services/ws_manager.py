"""In-memory WebSocket connection manager for live group chat.

Tracks the open sockets per conversation so a newly-sent message can be pushed
to everyone currently viewing that thread. Single-process only: if the API ever
runs on more than one replica, sockets on other instances won't receive the
broadcast and this needs a shared bus (e.g. Redis pub/sub) behind the same API.
"""

import asyncio
import logging
from collections import defaultdict

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self._conns: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, conversation_id: str, ws: WebSocket) -> None:
        """Register an already-accepted socket under a conversation."""
        async with self._lock:
            self._conns[conversation_id].add(ws)

    async def disconnect(self, conversation_id: str, ws: WebSocket) -> None:
        async with self._lock:
            conns = self._conns.get(conversation_id)
            if conns:
                conns.discard(ws)
                if not conns:
                    self._conns.pop(conversation_id, None)

    async def broadcast(self, conversation_id: str, payload: dict) -> None:
        """Send a JSON payload to every socket open on this conversation.

        Never raises: a socket that errors on send is assumed dead and dropped,
        so a broken client can't fail the HTTP request that triggered the push.
        """
        async with self._lock:
            targets = list(self._conns.get(conversation_id, ()))
        if not targets:
            return
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001 — a dead socket must not fail the sender
                dead.append(ws)
        if dead:
            async with self._lock:
                conns = self._conns.get(conversation_id)
                if conns:
                    for ws in dead:
                        conns.discard(ws)
                    if not conns:
                        self._conns.pop(conversation_id, None)


# Process-wide singleton.
manager = ConnectionManager()
