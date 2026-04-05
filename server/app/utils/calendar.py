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
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']


def _load_client_credentials() -> tuple[str | None, str | None]:
    """Extract client_id and client_secret from the OAuth secrets file.

    Returns (client_id, client_secret) or (None, None) if the file is
    missing or malformed.
    """
    if not os.path.exists(CLIENT_SECRETS_FILE):
        logger.warning("Cannot load client credentials — %s not found", CLIENT_SECRETS_FILE)
        return None, None

    try:
        with open(CLIENT_SECRETS_FILE) as f:
            data = json.load(f)
        # The JSON has a top-level key like "web" or "installed"
        key = next(iter(data))
        return data[key].get("client_id"), data[key].get("client_secret")
    except Exception as exc:
        logger.warning("Failed to parse %s: %s", CLIENT_SECRETS_FILE, exc)
        return None, None


def get_google_auth_flow(redirect_uri: str) -> Flow:
    """Initialize standard Google OAuth flow."""
    # Note: In production, check if CLIENT_SECRETS_FILE exists, otherwise handle gracefully
    if not os.path.exists(CLIENT_SECRETS_FILE):
        raise FileNotFoundError(f"OAuth credentials file not found: {CLIENT_SECRETS_FILE}")
        
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )
    return flow


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
