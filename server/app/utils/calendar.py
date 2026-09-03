import json
import logging

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import os

logger = logging.getLogger(__name__)

# You will need to download your Google OAuth Client ID JSON file and place it in the server directory
# Configure these for your environment
CLIENT_SECRETS_FILE = os.environ.get("GOOGLE_CLIENT_SECRETS_FILE", "client_secret.json")
# .readonly powers the existing busy-time display; .events is the narrowest
# scope that lets Jorna create/update/delete the one event per booking it
# writes back — not full calendar management. A vendor who connected before
# .events was requested holds a token scoped to .readonly only; Google does
# not silently upgrade a standing grant, so calendar_service persists exactly
# what was granted (Vendor.google_granted_scopes) and every write path checks
# it before attempting anything, rather than assuming this list is what any
# given vendor's token actually carries.
SCOPES = [
    'https://www.googleapis.com/auth/calendar.readonly',
    'https://www.googleapis.com/auth/calendar.events',
]


def _load_client_config() -> dict | None:
    """Return the parsed OAuth client config dict.

    Resolution order:
    1. client_secret.json file (local dev)
    2. GOOGLE_CLIENT_SECRETS_JSON env var (full JSON blob)
    3. GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET env vars (individual values)
    """
    if os.path.exists(CLIENT_SECRETS_FILE):
        try:
            with open(CLIENT_SECRETS_FILE) as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("Failed to parse %s: %s", CLIENT_SECRETS_FILE, exc)

    raw = os.environ.get("GOOGLE_CLIENT_SECRETS_JSON", "")
    if raw:
        try:
            return json.loads(raw)
        except Exception as exc:
            logger.warning("Failed to parse GOOGLE_CLIENT_SECRETS_JSON: %s", exc)

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if client_id and client_secret:
        return {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [],
            }
        }

    return None


def _load_client_credentials() -> tuple[str | None, str | None]:
    """Extract client_id and client_secret from available config sources."""
    data = _load_client_config()
    if not data:
        logger.warning(
            "Cannot load client credentials — set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET",
        )
        return None, None
    try:
        key = next(iter(data))
        return data[key].get("client_id"), data[key].get("client_secret")
    except Exception as exc:
        logger.warning("Failed to extract credentials from client config: %s", exc)
        return None, None


def get_google_auth_flow(redirect_uri: str) -> Flow:
    """Initialize Google OAuth flow from file, JSON blob, or individual env vars."""
    config = _load_client_config()
    if config is None:
        raise FileNotFoundError(
            "Google OAuth credentials not found. Set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET environment variables."
        )
    return Flow.from_client_config(config, scopes=SCOPES, redirect_uri=redirect_uri)


def create_google_calendar_service(
    access_token: str,
    refresh_token: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> tuple:
    """Create a Google Calendar API service instance using saved tokens.

    If *client_id* / *client_secret* are not supplied they are loaded
    automatically from the OAuth secrets file so that the google-auth
    library can refresh an expired access token transparently.

    Returns
    -------
    (service, credentials) — the caller should check ``credentials.token``
    after API calls to detect if a refresh occurred and persist the new token.
    """
    if not client_id or not client_secret:
        client_id, client_secret = _load_client_credentials()

    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token"
    )
    
    service = build('calendar', 'v3', credentials=creds)
    return service, creds

def get_freebusy_schedule(service, time_min: str, time_max: str, calendar_id: str = "primary"):
    """
    Fetch the free/busy schedule of the user's calendar.
    time_min and time_max must be RFC3339 formatted strings (e.g. '2026-03-01T00:00:00Z')
    """
    body = {
        "timeMin": time_min,
        "timeMax": time_max,
        "timeZone": "UTC",
        "items": [{"id": calendar_id}]
    }
    events_result = service.freebusy().query(body=body).execute()
    cal_dict = events_result.get('calendars', {})
    calendar_info = cal_dict.get(calendar_id, {})
    busy_intervals = calendar_info.get('busy', [])
    
    return busy_intervals
