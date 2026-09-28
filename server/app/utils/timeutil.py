"""Timestamps as the clients should read them.

The DateTime columns are stored naive and hold UTC. `.isoformat()` on one
gives "2026-09-28T14:37:00" with no offset, which every browser (and
Swift's ISO8601 parsing) reads as *local* time — the contract timeline
showed 2:37 PM for something that happened at 10:37 AM in New Jersey.
Serialize through utc_iso instead, so the offset travels with the value.
"""
from datetime import datetime, timezone


def utc_iso(value: datetime | str | None) -> str | None:
    """ISO 8601 with an explicit UTC offset. Accepts a naive UTC datetime,
    an aware one, or an ISO string already stored that way (the payment
    schedule keeps its marks as strings in JSON)."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
