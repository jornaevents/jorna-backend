"""Thin router for calendar/availability endpoints — delegates to calendar_service."""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import FRONTEND_URL, GOOGLE_OAUTH_REDIRECT_URI, WEB_APP_URL
from app.db.database import get_db
from app.db.models import User, Vendor
from app.dependencies import get_current_user
from app.services.calendar_service import (
    CalendarError,
    decode_state,
    get_google_auth_url as svc_get_google_auth_url,
    handle_google_callback as svc_handle_google_callback,
    get_vendor_availability as svc_get_vendor_availability,
)

router = APIRouter(prefix="/vendors", tags=["calendar"])


def _own_vendor(vendor_id: str, current_user: User, db: Session) -> Vendor:
    """The vendor, if the caller is the account behind it. 403 otherwise.

    Linking a calendar is an act on somebody's own business, and everything
    about the OAuth flow assumes that: the vendor_id in the state is where the
    resulting tokens land. Without this check, asking for an authorization URL
    under another vendor's id returns a properly signed one, and completing it
    with your own Google account writes your tokens onto their row — which
    marks their calendar busy with your life. Same rule the tag endpoints use.
    """
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    if vendor.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this vendor")
    return vendor


@router.get("/{vendor_id}/google-auth", summary="Get Google OAuth redirect URL")
def google_auth_url(
    vendor_id: str,
    client: str = Query(
        "ios",
        description="Which client is connecting: 'ios' returns to the app's "
        "bridge page, 'web' returns into the Jorna web app.",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Returns the URL the vendor should be redirected to in order to
    authorize Google Calendar access. Only the vendor's own account may ask.

    The same split as Stripe onboarding: iOS lands on this API's own page,
    which tells the vendor to return to the app; a browser has no app to return
    to, so ``client=web`` lands back in the web app instead. Which one is
    recorded in the signed state rather than taken from the callback, because
    by then the caller is Google — and a client-supplied return URL would be an
    open redirect.
    """
    _own_vendor(vendor_id, current_user, db)
    try:
        return svc_get_google_auth_url(
            vendor_id=vendor_id,
            redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
            client=client,
        )
    except CalendarError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


def _landing(
    client: str, *, success: bool, vendor_id: str = "", error: str = ""
) -> str:
    """Where to send the browser once Google is done with it.

    Both targets come from configuration, never from the request — the client
    only chooses between them. The web app is exported with trailing slashes,
    so its path carries one; the API's own bridge page does not.
    """
    base = f"{WEB_APP_URL}/calendar-connected/" if client == "web" else f"{FRONTEND_URL}/calendar-connected"
    params = {"success": "true" if success else "false"}
    if vendor_id:
        params["vendor_id"] = vendor_id
    if error:
        params["error"] = error
    return f"{base}?{urlencode(params)}"


@router.get("/auth/callback", summary="Google OAuth callback (handled server-side)")
def google_auth_callback(
    state: str,
    code: str,
    db: Session = Depends(get_db),
):
    """Google redirects the vendor here after they grant access.
    Decodes the state to recover vendor_id, code_verifier and the client that
    started the flow, exchanges the code for tokens, then sends the browser
    back where that client can receive it.
    """
    # Outside the try: a state we can't read is also a state that can't tell us
    # where to land, so a malformed one goes to the default and says so.
    try:
        vendor_id, code_verifier, client = decode_state(state)
    except CalendarError as e:
        return RedirectResponse(url=_landing("ios", success=False, error=e.detail))

    try:
        svc_handle_google_callback(
            vendor_id=vendor_id,
            code=code,
            redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
            code_verifier=code_verifier,
            db=db,
        )
        return RedirectResponse(url=_landing(client, success=True, vendor_id=vendor_id))
    except CalendarError as e:
        return RedirectResponse(url=_landing(client, success=False, error=e.detail))


@router.get("/{vendor_id}/calendar-status", summary="Check if Google Calendar is connected")
def calendar_status(
    vendor_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Returns whether the vendor has linked their Google Calendar.

    Their own account only: which accounts a vendor has linked is a fact about
    how they run their business, not something a browsing client needs. When a
    client wants to know when a vendor is free, that's /availability, which
    answers it in the terms the question was asked.
    """
    vendor = _own_vendor(vendor_id, current_user, db)
    return {"google_calendar_connected": bool(vendor.google_access_token)}


@router.get("/{vendor_id}/availability", summary="Get vendor open time slots")
def vendor_availability(
    vendor_id: str,
    start_date: str = Query(
        ..., description="ISO formatted start date (e.g. 2026-03-01T00:00:00Z)"
    ),
    end_date: str = Query(
        ..., description="ISO formatted end date (e.g. 2026-03-07T23:59:59Z)"
    ),
    db: Session = Depends(get_db),
):
    """Compute when a vendor is free based on baseline hours, Google Calendar,
    and existing Desiconnect bookings.
    """
    try:
        return svc_get_vendor_availability(
            vendor_id=vendor_id, start_date=start_date, end_date=end_date, db=db
        )
    except CalendarError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
