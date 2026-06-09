"""Transactional email via Resend.

A thin wrapper over the Resend HTTP API (https://resend.com). Used as a
fallback channel for booking notifications when a user has no FCM push token,
and for password-reset emails.

Gracefully no-ops (returns success=False) when RESEND_API_KEY is unset, so
local/dev environments and the test suite never make real network calls.
"""

import logging
from typing import Optional

import httpx

from app.config import EMAIL_FROM, RESEND_API_KEY

logger = logging.getLogger(__name__)

_RESEND_ENDPOINT = "https://api.resend.com/emails"


def _resend_key() -> Optional[str]:
    """Return the configured Resend API key, or None if unset/placeholder."""
    key = (RESEND_API_KEY or "").strip()
    return key or None


def send_email(
    *,
    to: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
    from_addr: Optional[str] = None,
) -> dict:
    """Send a single transactional email.

    Returns ``{"success": bool, "id"|"error": str}``. Never raises — a failed
    send is logged and reported in the return value so callers (notifications,
    password reset) can treat email as best-effort.
    """
    if not to:
        return {"success": False, "error": "No recipient email"}

    api_key = _resend_key()
    if not api_key:
        logger.info("RESEND_API_KEY not set — skipping email to %s (%r)", to, subject)
        return {"success": False, "error": "Email not configured"}

    payload = {
        "from": from_addr or EMAIL_FROM,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text

    try:
        resp = httpx.post(
            _RESEND_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15.0,
        )
        resp.raise_for_status()
        email_id = (resp.json() or {}).get("id")
        logger.info("Email sent → %s (id=%s)", to, email_id)
        return {"success": True, "id": email_id}
    except httpx.HTTPStatusError as exc:
        body = exc.response.text if exc.response is not None else ""
        logger.error("Resend send failed (%s): %s", exc.response.status_code if exc.response else "?", body)
        return {"success": False, "error": f"Resend error: {body[:200]}"}
    except Exception as exc:
        logger.error("Email send failed: %s", exc)
        return {"success": False, "error": str(exc)}
