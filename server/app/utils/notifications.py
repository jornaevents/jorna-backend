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


# ---------------------------------------------------------------------------
# High-level booking notification dispatchers
# ---------------------------------------------------------------------------

def notify_booking_status_change(
    *,
    status: str,
    booking_id: str,
    event_name: str,
    service_name: str,
    client_name: str,
    vendor_name: str,
    client_fcm_token: Optional[str] = None,
    vendor_fcm_token: Optional[str] = None,
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
    client_fcm_token, vendor_fcm_token :
        Firebase device tokens.  If None/empty the notification for that
        party is skipped silently.

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

    # --- Notify vendor ---
    vendor_result: dict = {"success": False, "error": "No vendor FCM token"}
    if vendor_fcm_token:
        vendor_result = send_push_notification(
            fcm_token=vendor_fcm_token,
            title=_fmt(templates["vendor_title"]),
            body=_fmt(templates["vendor_body"]),
            data=data_payload,
        )

    # --- Notify client ---
    client_result: dict = {"success": False, "error": "No client FCM token"}
    if client_fcm_token:
        client_result = send_push_notification(
            fcm_token=client_fcm_token,
            title=_fmt(templates["client_title"]),
            body=_fmt(templates["client_body"]),
            data=data_payload,
        )

    return {"client_result": client_result, "vendor_result": vendor_result}


def notify_check_in(
    *,
    booking_id: str,
    event_name: str,
    is_vendor: bool,
    client_name: str,
    vendor_name: str,
    recipient_fcm_token: Optional[str] = None,
) -> dict:
    """Notify the *other* party that someone has checked in at the venue."""
    if not recipient_fcm_token:
        return {"success": False, "error": "No recipient FCM token"}

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

    return send_push_notification(
        fcm_token=recipient_fcm_token,
        title=_fmt(template["title"]),
        body=_fmt(template["body"]),
        data={"booking_id": booking_id, "event": template_key},
    )
