"""Who sent a request, as far as the request itself can tell: the address
it came from and the browser's own description of itself. Recorded with a
contract view and signature (docs/DECISIONS.md #27).

request.client.host is the visitor's address because uvicorn trusts the
X-Forwarded-For that Railway's proxy sets (entrypoint.sh)."""

from fastapi import Request

USER_AGENT_MAX = 512


def client_meta(request: Request | None) -> dict:
    if request is None:
        return {"ip": None, "user_agent": None}
    ip = request.client.host if request.client else None
    user_agent = (request.headers.get("user-agent") or "").strip()[:USER_AGENT_MAX] or None
    return {"ip": ip, "user_agent": user_agent}
