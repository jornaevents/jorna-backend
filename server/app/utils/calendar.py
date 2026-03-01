from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import os

# You will need to download your Google OAuth Client ID JSON file and place it in the server directory
# Configure these for your environment
CLIENT_SECRETS_FILE = os.environ.get("GOOGLE_CLIENT_SECRETS_FILE", "client_secret.json")
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

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

def create_google_calendar_service(access_token: str, refresh_token: str = None, client_id: str = None, client_secret: str = None):
    """Create a Google Calendar API service instance using saved tokens."""
    # Assuming basic credentials structure 
    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token"
    )
    
    return build('calendar', 'v3', credentials=creds)

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
