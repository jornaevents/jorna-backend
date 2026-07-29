"""Resolving the clock a celebration runs on.

Checked against real places rather than invented coordinates, because the whole
difficulty here is that the boundaries are where they are and not where a
meridian would put them. The cities in the split states are the ones that a
longitude-only rule gets wrong.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services.timezone_service import (
    CENTRAL,
    EASTERN,
    MOUNTAIN,
    PACIFIC,
    local_to_utc,
    state_from,
    zone_name_for,
)


def zone(location, lat=None, lng=None):
    return zone_name_for(location=location, latitude=lat, longitude=lng)


# ── Reading the address ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "address,expected",
    [
        ("12 Maple Ave, Evanston, IL 60201", "IL"),
        ("12 Maple Ave, Evanston, IL", "IL"),
        ("Evanston, IL 60201-1234", "IL"),
        ("  740 Fifth Ave, New York, NY 10019  ", "NY"),
        ("", None),
        (None, None),
        ("Somewhere", None),
    ],
)
def test_the_state_is_read_off_the_end_of_the_address(address, expected):
    assert state_from(address) == expected


def test_two_capitals_earlier_in_the_address_are_not_the_state():
    """"MD Banquet Hall, Fremont, CA" is in California, not Maryland."""
    assert state_from("MD Banquet Hall, Fremont, CA 94538") == "CA"
    assert state_from("OK Corral Events, Austin, TX 78701") == "TX"


# ── The states a line doesn't cross ───────────────────────────────────


@pytest.mark.parametrize(
    "city,lat,lng,expected",
    [
        ("Chicago, IL 60601", 41.88, -87.63, CENTRAL),
        ("Edison, NJ 08817", 40.52, -74.41, EASTERN),
        ("Atlanta, GA 30303", 33.75, -84.39, EASTERN),
        ("Fremont, CA 94538", 37.55, -121.99, PACIFIC),
        ("Denver, CO 80202", 39.74, -104.99, MOUNTAIN),
        ("Seattle, WA 98101", 47.61, -122.33, PACIFIC),
        ("Phoenix, AZ 85004", 33.45, -112.07, "America/Phoenix"),
        ("Honolulu, HI 96813", 21.31, -157.86, "Pacific/Honolulu"),
    ],
)
def test_unambiguous_states(city, lat, lng, expected):
    assert zone(city, lat, lng) == expected


# ── The thirteen a line does cross ────────────────────────────────────


@pytest.mark.parametrize(
    "city,lat,lng,expected",
    [
        # Florida: the peninsula against the panhandle.
        ("Orlando, FL 32801", 28.54, -81.38, EASTERN),
        ("Miami, FL 33130", 25.77, -80.19, EASTERN),
        ("Pensacola, FL 32502", 30.42, -87.22, CENTRAL),
        # Texas: everything except the far west corner.
        ("Houston, TX 77002", 29.76, -95.37, CENTRAL),
        ("Dallas, TX 75201", 32.78, -96.80, CENTRAL),
        ("El Paso, TX 79901", 31.76, -106.49, MOUNTAIN),
        # Tennessee: Chattanooga and Knoxville keep New York's clock.
        ("Nashville, TN 37203", 36.16, -86.78, CENTRAL),
        ("Memphis, TN 38103", 35.15, -90.05, CENTRAL),
        ("Knoxville, TN 37902", 35.96, -83.92, EASTERN),
        ("Chattanooga, TN 37402", 35.05, -85.31, EASTERN),
        # Kentucky: Louisville is Eastern despite sitting west of Chattanooga.
        ("Louisville, KY 40202", 38.25, -85.76, EASTERN),
        ("Lexington, KY 40507", 38.05, -84.50, EASTERN),
        ("Bowling Green, KY 42101", 36.99, -86.44, CENTRAL),
        ("Paducah, KY 42001", 37.08, -88.60, CENTRAL),
        # Indiana: Eastern but for two corners.
        ("Indianapolis, IN 46204", 39.77, -86.16, EASTERN),
        ("Fort Wayne, IN 46802", 41.08, -85.14, EASTERN),
        ("Gary, IN 46402", 41.60, -87.34, CENTRAL),
        ("Evansville, IN 47708", 37.97, -87.57, CENTRAL),
        # Michigan: the far west of the Upper Peninsula.
        ("Detroit, MI 48226", 42.33, -83.05, EASTERN),
        ("Marquette, MI 49855", 46.54, -87.40, EASTERN),
        ("Iron Mountain, MI 49801", 45.82, -88.07, CENTRAL),
        # The plains states, split down their western thirds.
        ("Bismarck, ND 58501", 46.81, -100.78, CENTRAL),
        ("Dickinson, ND 58601", 46.88, -102.79, MOUNTAIN),
        ("Sioux Falls, SD 57104", 43.55, -96.73, CENTRAL),
        ("Rapid City, SD 57701", 44.08, -103.23, MOUNTAIN),
        ("Omaha, NE 68102", 41.26, -95.94, CENTRAL),
        ("Scottsbluff, NE 69361", 41.87, -103.66, MOUNTAIN),
        ("Wichita, KS 67202", 37.69, -97.34, CENTRAL),
        ("Goodland, KS 67735", 39.35, -101.71, MOUNTAIN),
        # The north-west.
        ("Portland, OR 97204", 45.52, -122.68, PACIFIC),
        ("Ontario, OR 97914", 44.03, -116.96, MOUNTAIN),
        ("Boise, ID 83702", 43.62, -116.20, MOUNTAIN),
        ("Coeur d'Alene, ID 83814", 47.68, -116.78, PACIFIC),
        ("Las Vegas, NV 89101", 36.17, -115.14, PACIFIC),
    ],
)
def test_split_states(city, lat, lng, expected):
    assert zone(city, lat, lng) == expected


# ── When it can't be told ─────────────────────────────────────────────


def test_a_split_state_with_no_coordinates_is_unknown():
    """Longitude is the only thing separating the halves — without it there is
    nothing to guess with, and a guess would be an hour wrong half the time."""
    assert zone("Nashville, TN 37203") is None
    assert zone("Indianapolis, IN") is None


def test_nothing_at_all_is_unknown():
    assert zone(None) is None
    assert zone("Somewhere nice") is None


def test_coordinates_alone_fall_back_to_longitude():
    """Coarse, and only ever reached when an address has no state on it."""
    assert zone("Somewhere nice", 41.88, -87.63) == CENTRAL
    assert zone(None, 40.71, -74.01) == EASTERN


# ── Turning a stored time into a real moment ──────────────────────────


def test_a_chicago_evening_is_not_a_chicago_morning():
    """The bug this whole module exists to prevent: read "18:00" as UTC and a
    reminder for a six o'clock reception goes out at half past eleven that
    morning."""
    moment = local_to_utc(
        date_iso="2027-06-05", time_hhmm="18:00",
        location="12 Maple Ave, Evanston, IL 60201", latitude=42.05, longitude=-87.69,
    )
    # June: Chicago is on daylight time, UTC-5.
    assert moment == datetime(2027, 6, 5, 23, 0, tzinfo=timezone.utc)


def test_daylight_saving_is_taken_from_the_date():
    """Same place, same clock time, two hours of the year — one hour apart in
    real time. A fixed offset would get one of them wrong."""
    summer = local_to_utc(
        date_iso="2027-07-04", time_hhmm="18:00",
        location="Chicago, IL 60601", latitude=41.88, longitude=-87.63,
    )
    winter = local_to_utc(
        date_iso="2027-01-09", time_hhmm="18:00",
        location="Chicago, IL 60601", latitude=41.88, longitude=-87.63,
    )
    assert summer.hour == 23  # UTC-5
    assert winter.hour == 0 and winter.day == 10  # UTC-6, and over midnight
    assert winter.astimezone(ZoneInfo(CENTRAL)).hour == 18


def test_arizona_does_not_move_with_the_clocks():
    summer = local_to_utc(
        date_iso="2027-07-04", time_hhmm="18:00",
        location="Phoenix, AZ 85004", latitude=33.45, longitude=-112.07,
    )
    winter = local_to_utc(
        date_iso="2027-01-09", time_hhmm="18:00",
        location="Phoenix, AZ 85004", latitude=33.45, longitude=-112.07,
    )
    assert summer.hour == winter.hour == 1  # UTC-7 all year


@pytest.mark.parametrize(
    "date_iso,time_hhmm",
    [
        (None, "18:00"),
        ("TBD", "18:00"),
        ("2027-06-05", None),
        ("2027-06-05", "not a time"),
        ("2027-06-05", "25:00"),
        ("not a date", "18:00"),
    ],
)
def test_an_unreadable_moment_is_no_moment(date_iso, time_hhmm):
    assert (
        local_to_utc(
            date_iso=date_iso, time_hhmm=time_hhmm,
            location="Chicago, IL 60601", latitude=41.88, longitude=-87.63,
        )
        is None
    )


def test_a_place_with_no_timezone_has_no_moment():
    assert (
        local_to_utc(
            date_iso="2027-06-05", time_hhmm="18:00",
            location="Somewhere", latitude=None, longitude=None,
        )
        is None
    )


def test_seconds_on_the_time_are_tolerated():
    assert local_to_utc(
        date_iso="2027-06-05", time_hhmm="18:00:00",
        location="Chicago, IL 60601", latitude=41.88, longitude=-87.63,
    ) == datetime(2027, 6, 5, 23, 0, tzinfo=timezone.utc)
