"""An address turned into a map pin, server-side.

Check-in is measured against a point. Until now the only thing that produced one
for an event was the browser: the web app calls the US Census geocoder itself
(web/src/lib/geocode.ts) when a client edits the address, and writes the result
back with the address.

Every other route left the point behind. The bundle builder writes a location
onto its bookings and _ensure_bundle_event copies it onto the event; POST
/bookings does the same; iOS writes free text. All of those produce a plan with
a perfectly good address and no coordinates — and then checkin_anchor finds
nothing, so nobody can check in, no check-in reminder goes out, and the client's
plan says "there's nowhere to check in against until the plan has an address"
underneath the address.

Doing it here fixes it for every route at once, including the phone.

Same free Census service the web uses, and for the same reasons: no key, no
account, no quota worth counting, and it is the data the government addresses
against. US-only, which is what this marketplace is. Out here there is no CORS
problem and no JSONP — it is an ordinary GET.
"""

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Set in the test suite. Without it every test that reads a plan or saves an
# address would reach out to a government server: slow, and a suite that fails
# when the wifi drops is a suite people learn to ignore. Off means "no pin",
# which is a state the callers already handle.
_DISABLED = os.getenv("JORNA_DISABLE_GEOCODE") == "1"

_ENDPOINT = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
# Short on purpose. This runs inside a request that has something else to do,
# and a plan that draws without a pin is a smaller failure than one that doesn't
# draw at all.
_TIMEOUT_S = 6.0


def geocode_us_address(address: Optional[str]) -> Optional[tuple[float, float]]:
    """(lat, lng) for a US address, or None.

    None covers both "no confident match" and "couldn't ask" — the caller does
    the same thing either way, which is to carry on without a pin.
    """
    query = (address or "").strip()
    if not query or _DISABLED:
        return None

    try:
        response = httpx.get(
            _ENDPOINT,
            params={
                "address": query,
                "benchmark": "Public_AR_Current",
                "format": "json",
            },
            timeout=_TIMEOUT_S,
        )
        response.raise_for_status()
        matches = (response.json().get("result") or {}).get("addressMatches") or []
        if not matches:
            return None
        coords = matches[0].get("coordinates") or {}
        lat, lng = coords.get("y"), coords.get("x")
        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
            return float(lat), float(lng)
        return None
    except Exception as exc:  # noqa: BLE001 — network, JSON, shape, all the same here
        logger.info("Geocode failed for %r: %s", query, exc)
        return None


def backfill_event_pin(event, db) -> bool:
    """Give an event coordinates from its own address, if it hasn't got any.

    Returns whether it wrote anything. Cheap to call on an event that already
    has a pin — it does nothing — which is what makes it safe to put on the path
    that reads one plan.

    Deliberately not called while listing many plans: that would be a network
    call per event, on a screen whose whole point is to be one query.
    """
    if event is None or event.address_latitude is not None:
        return False
    if not (event.location or "").strip():
        return False

    pin = geocode_us_address(event.location)
    if not pin:
        return False

    event.address_latitude, event.address_longitude = pin
    db.commit()
    logger.info(
        "Backfilled pin for event %s from its address", getattr(event, "event_id", "?")
    )
    return True
