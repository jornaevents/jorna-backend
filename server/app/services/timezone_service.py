"""What time it is where the celebration is.

Bookings store a date and a wall-clock time — "2027-06-05", "18:00" — and
nothing about where on earth that clock is. For most of what this app does that
doesn't matter: comparing two bookings for an overlap works fine as long as both
are read the same way, which is why calendar_service can get away with reading
them as UTC.

It matters completely for anything that has to happen at a real moment. A
reminder half an hour before a six o'clock reception in Chicago goes out at
23:30 UTC. Read the same string as UTC and it goes out at half past eleven in
the morning, local — six hours early, to a vendor who is nowhere near the venue
and now trusts the app slightly less.

So this resolves a US timezone from what a booking already carries: the state in
its address, and the venue's coordinates. State first, because for thirty-seven
states it is simply correct. The thirteen states a timezone line runs through
are resolved by longitude against the boundary, which is exact for every
metropolitan area the marketplace serves and approximate in a handful of rural
counties — the line follows county borders, not a meridian.

When neither signal is there, this returns None and the caller does nothing.
A reminder that doesn't arrive is a missing convenience. One that arrives at
three in the morning is a reason to turn reminders off.
"""

import logging
import re
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

EASTERN = "America/New_York"
CENTRAL = "America/Chicago"
MOUNTAIN = "America/Denver"
ARIZONA = "America/Phoenix"      # Mountain standard time, no daylight saving
PACIFIC = "America/Los_Angeles"
ALASKA = "America/Anchorage"
HAWAII = "Pacific/Honolulu"      # no daylight saving

# States a timezone line does not run through.
_WHOLE_STATE = {
    "CT": EASTERN, "DC": EASTERN, "DE": EASTERN, "GA": EASTERN, "MA": EASTERN,
    "MD": EASTERN, "ME": EASTERN, "NC": EASTERN, "NH": EASTERN, "NJ": EASTERN,
    "NY": EASTERN, "OH": EASTERN, "PA": EASTERN, "RI": EASTERN, "SC": EASTERN,
    "VA": EASTERN, "VT": EASTERN, "WV": EASTERN,

    "AL": CENTRAL, "AR": CENTRAL, "IA": CENTRAL, "IL": CENTRAL, "LA": CENTRAL,
    "MN": CENTRAL, "MS": CENTRAL, "MO": CENTRAL, "OK": CENTRAL, "WI": CENTRAL,

    "CO": MOUNTAIN, "MT": MOUNTAIN, "NM": MOUNTAIN, "UT": MOUNTAIN, "WY": MOUNTAIN,

    "AZ": ARIZONA,
    "CA": PACIFIC, "WA": PACIFIC,
    "AK": ALASKA,
    "HI": HAWAII,
    "PR": "America/Puerto_Rico",
}


_SPLIT_STATES = {"FL", "TX", "TN", "KY", "IN", "MI", "ND", "SD", "NE", "KS", "OR", "ID", "NV"}


def _split_state(state: str, lat: Optional[float], lng: Optional[float]) -> Optional[str]:
    """The thirteen states a timezone boundary crosses.

    Each rule is the majority zone plus the condition for the minority side,
    checked against the venue's own coordinates. The boundaries below are the
    longitude that best separates the populated places on either side — the real
    line follows county borders, so a rural county straddling it can land an
    hour out. Every metro on either side of every one of these is correct.
    """
    if lng is None:
        return None

    if state == "FL":
        # The panhandle west of the Apalachicola is Central.
        return CENTRAL if lng < -85.0 else EASTERN
    if state == "TX":
        # El Paso and Hudspeth, and nothing else.
        return MOUNTAIN if lng < -105.0 else CENTRAL
    if state == "TN":
        # Chattanooga and Knoxville east; Nashville and Memphis west.
        return EASTERN if lng > -85.4 else CENTRAL
    if state == "KY":
        # Louisville and Lexington east; Bowling Green and Paducah west.
        return EASTERN if lng > -85.9 else CENTRAL
    if state == "IN":
        # Eastern but for two corners: Gary in the north-west and Evansville in
        # the south-west, both of which keep Chicago's clock.
        if lng < -87.0 and lat is not None and (lat > 41.0 or lat < 38.5):
            return CENTRAL
        return EASTERN
    if state == "MI":
        # Four counties at the west end of the Upper Peninsula.
        if lng < -88.0 and lat is not None and lat > 45.0:
            return CENTRAL
        return EASTERN
    if state == "ND":
        return MOUNTAIN if lng < -100.8 else CENTRAL
    if state == "SD":
        return MOUNTAIN if lng < -100.0 else CENTRAL
    if state == "NE":
        return MOUNTAIN if lng < -101.0 else CENTRAL
    if state == "KS":
        return MOUNTAIN if lng < -101.5 else CENTRAL
    if state == "OR":
        # Malheur County, in the south-east corner, runs on Boise's clock.
        if lng > -118.0 and lat is not None and lat < 44.3:
            return MOUNTAIN
        return PACIFIC
    if state == "ID":
        # Split roughly along the Salmon River: the panhandle is Pacific.
        if lat is not None and lat > 45.5:
            return PACIFIC
        return MOUNTAIN
    if state == "NV":
        # West Wendover, on the Utah line.
        if lng > -114.1 and lat is not None and lat > 40.5:
            return MOUNTAIN
        return PACIFIC
    return None


# "12 Maple Ave, Evanston, IL 60201" — and the same without the postcode.
_STATE_IN_ADDRESS = re.compile(r"\b([A-Z]{2})\b(?:\s*,)?\s*(?:\d{5}(?:-\d{4})?)?\s*$")


def state_from(location: Optional[str]) -> Optional[str]:
    """The two-letter state at the end of an address, if there is one.

    Anchored to the end rather than searched for, so a street or a venue name
    that happens to contain two capitals — "MD Banquet Hall, Fremont, CA" — is
    read as California rather than Maryland.
    """
    if not location:
        return None
    match = _STATE_IN_ADDRESS.search(location.strip().upper())
    if not match:
        return None
    state = match.group(1)
    return state if state in _WHOLE_STATE or state in _SPLIT_STATES else None


def _by_longitude(lng: float) -> str:
    """Last resort, when an address carries no state.

    Coarse: the real boundaries wander by hundreds of miles and this puts them
    at a meridian. Good enough to be better than nothing, and never used when a
    state is available.
    """
    if lng > -85.0:
        return EASTERN
    if lng > -101.0:
        return CENTRAL
    if lng > -115.0:
        return MOUNTAIN
    return PACIFIC


def zone_name_for(
    *, location: Optional[str], latitude: Optional[float], longitude: Optional[float],
) -> Optional[str]:
    """The IANA zone this celebration's clock runs on, or None if it can't be told."""
    state = state_from(location)
    if state:
        whole = _WHOLE_STATE.get(state)
        if whole:
            return whole
        split = _split_state(state, latitude, longitude)
        if split:
            return split
        # A split state with no coordinates to place it in. Longitude is the
        # only thing that separates the two halves, so there is nothing to
        # guess with.
        return None
    if longitude is not None:
        return _by_longitude(longitude)
    return None


def zone_for(
    *, location: Optional[str], latitude: Optional[float], longitude: Optional[float],
) -> Optional[ZoneInfo]:
    name = zone_name_for(location=location, latitude=latitude, longitude=longitude)
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # pragma: no cover - only if tzdata is missing
        logger.warning("No zoneinfo for %s", name)
        return None


def local_to_utc(
    *,
    date_iso: Optional[str],
    time_hhmm: Optional[str],
    location: Optional[str],
    latitude: Optional[float],
    longitude: Optional[float],
) -> Optional[datetime]:
    """The real moment a stored date and wall-clock time refer to.

    None when any part of that can't be established — an unset date, a time
    that won't parse, or a place with no timezone. Every one of those means
    "don't know when this is", and the callers treat it that way.
    """
    if not date_iso or date_iso == "TBD" or not time_hhmm:
        return None

    zone = zone_for(location=location, latitude=latitude, longitude=longitude)
    if zone is None:
        return None

    match = re.match(r"^(\d{1,2}):(\d{2})", time_hhmm.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None

    try:
        day = datetime.strptime(date_iso.strip(), "%Y-%m-%d")
    except ValueError:
        return None

    # fold=0 picks the first of a repeated hour when the clocks go back, which
    # is the earlier real moment — the safe side for a reminder.
    local = day.replace(hour=hour, minute=minute, tzinfo=zone, fold=0)
    return local.astimezone(ZoneInfo("UTC"))
