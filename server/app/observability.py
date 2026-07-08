"""Error monitoring via Sentry.

Enabled only when ``SENTRY_DSN`` is set — otherwise every function here is a
no-op, so local development and unconfigured deploys run exactly as before.
``init_sentry()`` is called as early as possible in ``main.py`` (before routers
import) so startup and import-time errors are captured too.
"""

import logging

from app.config import (
    RELEASE,
    SENTRY_DSN,
    SENTRY_ENVIRONMENT,
    SENTRY_TRACES_SAMPLE_RATE,
)

logger = logging.getLogger(__name__)

# Request headers that must never reach Sentry, on top of send_default_pii=False.
_SENSITIVE_HEADERS = {"authorization", "cookie", "x-api-key"}


def _scrub(event: dict, hint: dict) -> dict:
    """before_send hook: strip auth headers/cookies from outgoing events.

    Defence in depth over ``send_default_pii=False`` — we never want a bearer
    token or session cookie sitting in an error report.
    """
    request = event.get("request")
    if isinstance(request, dict):
        headers = request.get("headers")
        if isinstance(headers, dict):
            for name in list(headers):
                if name.lower() in _SENSITIVE_HEADERS:
                    headers[name] = "[Filtered]"
        request.pop("cookies", None)
    return event


def init_sentry() -> bool:
    """Initialise Sentry when configured. Returns True if enabled.

    No-op (returns False) when ``SENTRY_DSN`` is unset or the SDK isn't
    installed, so the app never fails to boot on account of monitoring.
    """
    if not SENTRY_DSN:
        logger.info("Sentry disabled — SENTRY_DSN not set.")
        return False

    try:
        import sentry_sdk
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; skipping.")
        return False

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        release=RELEASE,
        # Capture all errors; sample a fraction of requests for performance traces.
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        # Don't attach PII (user IP, cookies, request bodies); _scrub handles headers.
        send_default_pii=False,
        before_send=_scrub,
    )
    logger.info("Sentry initialised (environment=%s).", SENTRY_ENVIRONMENT)
    return True
