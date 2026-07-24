"""
Firebase Cloud Messaging (FCM) notification wrapper.

Sends push notifications to users/vendors when booking events occur
(e.g. pending, approved, rejected, payment_confirmed, check-in).

Setup
-----
1. Go to the Firebase Console → Project Settings → Service Accounts.
2. Click "Generate new private key" to download a JSON credentials file.
3. Place that file in the server directory and either:
   a. Set the env var  FIREBASE_CREDENTIALS_PATH=path/to/firebase_creds.json
   b. Or rename it to  firebase_credentials.json  in the server/ root.
"""

import logging
import os
from typing import Optional

from app.models.schemas import BookingStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Firebase Admin SDK initialisation (lazy / singleton)
# ---------------------------------------------------------------------------
_firebase_app = None  # Will be set on first call to _ensure_firebase()


def _ensure_firebase():
    """Initialise the Firebase Admin SDK exactly once.

    Returns True if Firebase is ready, False otherwise.
    """
    global _firebase_app

    if _firebase_app is not None:
        return True

    try:
        import firebase_admin  # type: ignore
        from firebase_admin import credentials as fb_credentials  # type: ignore
    except ImportError:
        logger.warning(
            "firebase-admin package is not installed. "
            "Run:  pip install firebase-admin"
        )
        return False

    creds_path = os.environ.get(
        "FIREBASE_CREDENTIALS_PATH", "firebase_credentials.json"
    )

    if not os.path.exists(creds_path):
        logger.warning(
            "Firebase credentials file not found at '%s'. "
            "Push notifications are DISABLED.",
            creds_path,
        )
        return False

    try:
        cred = fb_credentials.Certificate(creds_path)
        # Check if a default app already exists (e.g. from a previous init)
        try:
            _firebase_app = firebase_admin.get_app()
            logger.info("Firebase Admin SDK already initialised — reusing.")
        except ValueError:
            _firebase_app = firebase_admin.initialize_app(cred)
            logger.info("Firebase Admin SDK initialised successfully.")
        return True
    except Exception as exc:
        logger.error("Failed to initialise Firebase: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Message templates per booking status
# ---------------------------------------------------------------------------

# Each entry maps a BookingStatus to (title, body_template).
# {vendor_name}, {client_name}, {event_name}, {service_name} are replaced
# at send-time.
NOTIFICATION_TEMPLATES: dict[str, dict[str, str]] = {
    BookingStatus.PENDING.value: {
        "vendor_title": "📥 New Booking Request",
        "vendor_body": "{client_name} has requested '{service_name}' for {event_name}.",
        "client_title": "⏳ Booking Submitted",
        "client_body": "Your booking for '{service_name}' at {event_name} has been submitted. Waiting for vendor confirmation.",
    },
    BookingStatus.APPROVED.value: {
        "vendor_title": "✅ Booking Confirmed",
        "vendor_body": "You approved '{service_name}' for {event_name}.",
        "client_title": "🎉 Booking Approved!",
        "client_body": "{vendor_name} has approved your booking for '{service_name}' at {event_name}!",
    },
    BookingStatus.REJECTED.value: {
        "vendor_title": "❌ Booking Declined",
        "vendor_body": "You declined '{service_name}' for {event_name}.",
        "client_title": "😔 Booking Declined",
        "client_body": "{vendor_name} was unable to accept your booking for '{service_name}' at {event_name}.",
    },
    BookingStatus.PAYMENT_CONFIRMED.value: {
        "vendor_title": "💰 Payment Received",
        "vendor_body": "Payment confirmed for '{service_name}' at {event_name}.",
        "client_title": "💳 Payment Confirmed",
        "client_body": "Your payment for '{service_name}' at {event_name} has been confirmed.",
    },
}

# Separate template for check-in events (not a BookingStatus state)
CHECKIN_TEMPLATES = {
    "vendor_checkin": {
        "title": "📍 Vendor Checked In",
        "body": "{vendor_name} has arrived at the venue for {event_name}.",
    },
    "client_checkin": {
        "title": "📍 Client Checked In",
        "body": "{client_name} has arrived at the venue for {event_name}.",
    },
}


# ---------------------------------------------------------------------------
# Core send helpers
# ---------------------------------------------------------------------------

def _build_message(
    fcm_token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
):
    """Build a Firebase ``messaging.Message`` object."""
    from firebase_admin import messaging  # type: ignore

    notification = messaging.Notification(title=title, body=body)

    # APNs config for iOS badges / sounds
    apns = messaging.APNSConfig(
        payload=messaging.APNSPayload(
            aps=messaging.Aps(sound="default", badge=1),
        )
    )

    return messaging.Message(
        notification=notification,
        token=fcm_token,
        data=data or {},
        apns=apns,
    )


def send_push_notification(
    fcm_token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> dict:
    """Send a single push notification to one FCM token.

    Returns a dict with ``success`` (bool), and ``message_id`` or ``error``.
    """
    if not fcm_token:
        return {"success": False, "error": "No FCM token provided"}

    if not _ensure_firebase():
        return {"success": False, "error": "Firebase not configured"}

    from firebase_admin import messaging  # type: ignore

    try:
        message = _build_message(fcm_token, title, body, data)
        message_id = messaging.send(message)
        logger.info("Push sent → %s  (token=%s…)", message_id, fcm_token[:12])
        return {"success": True, "message_id": message_id}
    except messaging.UnregisteredError:
        logger.warning("FCM token is unregistered: %s…", fcm_token[:12])
        return {"success": False, "error": "Token unregistered"}
    except Exception as exc:
        logger.error("FCM send failed: %s", exc)
        return {"success": False, "error": str(exc)}


def send_push_to_multiple(
    fcm_tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> list[dict]:
    """Send the same notification to multiple tokens.  Returns a list of results."""
    return [
        send_push_notification(token, title, body, data) for token in fcm_tokens
    ]


def send_push_to_user(
    user,
    title: str,
    body: str,
    data: Optional[dict] = None,
    *,
    db,
) -> dict:
    """Send a push to every device a user has registered, and prune any token FCM
    reports as unregistered (app uninstalled, browser permission revoked).

    One user has many devices — a phone plus one or more browsers — so this fans
    out across all of them. Returns ``{"sent": <delivered>, "devices": <tried>}``;
    ``sent > 0`` means at least one device got it (used to decide email fallback).
    """
    if user is None:
        return {"sent": 0, "devices": 0}

    from app.db.models import PushToken

    tokens = db.query(PushToken).filter(PushToken.user_id == user.user_id).all()
    sent = 0
    dead: list = []
    for pt in tokens:
        result = send_push_notification(pt.token, title, body, data)
        if result.get("success"):
            sent += 1
        elif result.get("error") == "Token unregistered":
            dead.append(pt)

    if dead:
        for pt in dead:
            db.delete(pt)
        db.commit()

    return {"sent": sent, "devices": len(tokens)}


# ---------------------------------------------------------------------------
# Email fallback
# ---------------------------------------------------------------------------

def _email_html(title: str, body: str) -> str:
    """Wrap a notification title/body in a minimal branded HTML email."""
    return (
        '<div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;'
        'padding:24px;color:#1a1a1a">'
        f'<h2 style="margin:0 0 12px">{title}</h2>'
        f'<p style="font-size:15px;line-height:1.5;margin:0 0 20px">{body}</p>'
        '<hr style="border:none;border-top:1px solid #eee;margin:20px 0">'
        '<p style="font-size:12px;color:#888;margin:0">Desiconnect — your South Asian event marketplace.</p>'
        '</div>'
    )


def _send_booking_email(to_email: Optional[str], title: str, body: str) -> dict:
    """Send a booking notification email. Best-effort; never raises."""
    if not to_email:
        return {"success": False, "error": "No recipient email"}
    try:
        from app.services.email_service import send_email
        return send_email(to=to_email, subject=title, html=_email_html(title, body), text=body)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Booking email failed: %s", exc)
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# High-level booking notification dispatchers
# ---------------------------------------------------------------------------

def notify_booking_status_change(
    *,
    status: str,
    booking_id: str,
    event_name: str = "Event",
    service_name: str,
    client_name: str,
    vendor_name: str,
    client_user=None,
    vendor_user=None,
    db,
) -> dict:
    """Send push notifications to the relevant parties when a booking's
    status changes.

    Parameters
    ----------
    status : str
        One of the BookingStatus values: pending, approved, rejected,
        payment_confirmed.
    booking_id, event_name, service_name, client_name, vendor_name :
        Human-readable context injected into the message body.
    client_user, vendor_user :
        The User rows. Each is pushed to on all their devices; if none received
        it (no device, or delivery failed) their email is used as a fallback.
        A None party is skipped silently.
    db :
        Session — needed to look up each party's devices and prune dead tokens.

    Returns
    -------
    dict with ``client_result`` and ``vendor_result`` sub-dicts.
    """
    templates = NOTIFICATION_TEMPLATES.get(status)
    if templates is None:
        return {
            "client_result": {"success": False, "error": f"No template for status '{status}'"},
            "vendor_result": {"success": False, "error": f"No template for status '{status}'"},
        }

    replacements = {
        "{client_name}": client_name,
        "{vendor_name}": vendor_name,
        "{event_name}": event_name,
        "{service_name}": service_name,
    }

    def _fmt(text: str) -> str:
        for placeholder, value in replacements.items():
            text = text.replace(placeholder, value)
        return text

    data_payload = {"booking_id": booking_id, "status": status}

    # --- Notify vendor (push to all devices, with email fallback) ---
    vendor_result: dict = {"sent": 0, "devices": 0}
    if vendor_user is not None:
        vendor_result = send_push_to_user(
            vendor_user,
            _fmt(templates["vendor_title"]),
            _fmt(templates["vendor_body"]),
            data_payload,
            db=db,
        )
    vendor_email_result = None
    if vendor_result.get("sent", 0) == 0 and vendor_user is not None and vendor_user.email:
        vendor_email_result = _send_booking_email(
            vendor_user.email, _fmt(templates["vendor_title"]), _fmt(templates["vendor_body"])
        )

    # --- Notify client (push to all devices, with email fallback) ---
    client_result: dict = {"sent": 0, "devices": 0}
    if client_user is not None:
        client_result = send_push_to_user(
            client_user,
            _fmt(templates["client_title"]),
            _fmt(templates["client_body"]),
            data_payload,
            db=db,
        )
    client_email_result = None
    if client_result.get("sent", 0) == 0 and client_user is not None and client_user.email:
        client_email_result = _send_booking_email(
            client_user.email, _fmt(templates["client_title"]), _fmt(templates["client_body"])
        )

    return {
        "client_result": client_result,
        "vendor_result": vendor_result,
        "client_email_result": client_email_result,
        "vendor_email_result": vendor_email_result,
    }


def notify_check_in(
    *,
    booking_id: str,
    event_name: str = "Event",
    is_vendor: bool,
    client_name: str,
    vendor_name: str,
    recipient_user=None,
    db,
) -> dict:
    """Notify the *other* party (on all their devices) that someone checked in."""
    if recipient_user is None:
        return {"sent": 0, "devices": 0, "error": "No recipient"}

    template_key = "vendor_checkin" if is_vendor else "client_checkin"
    template = CHECKIN_TEMPLATES[template_key]

    replacements = {
        "{client_name}": client_name,
        "{vendor_name}": vendor_name,
        "{event_name}": event_name,
    }

    def _fmt(text: str) -> str:
        for placeholder, value in replacements.items():
            text = text.replace(placeholder, value)
        return text

    return send_push_to_user(
        recipient_user,
        _fmt(template["title"]),
        _fmt(template["body"]),
        {"booking_id": booking_id, "event": template_key},
        db=db,
    )
