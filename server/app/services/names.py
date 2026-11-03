"""How a vendor is named to everyone else.

A vendor's business name when they've given one (Vendor.business_name, 0082),
else the account holder's own first and last name — which is all there was
before. Every place a client, a guest, an email or a contract names the
vendor goes through here, so a storefront, a signing page and a PDF never
disagree about who the vendor is.

Not for talking *to* the vendor: an email that greets them still uses their
own first name.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.models import User, Vendor


def person_name(user: "User | None") -> str:
    return f"{user.f_name or ''} {user.l_name or ''}".strip() if user else ""


def vendor_display_name(vendor: "Vendor | None", user: "User | None", fallback: str | None = None) -> str | None:
    """The business name, else the person's name, else ``fallback``."""
    business = (getattr(vendor, "business_name", None) or "").strip()
    return business or person_name(user) or fallback


def vendor_user_display_name(user: "User | None", fallback: str | None = None) -> str | None:
    """vendor_display_name for a caller holding only the vendor's User — the
    Vendor row is looked up through the same session."""
    if user is None:
        return fallback
    from sqlalchemy.orm import object_session

    from app.db.models import Vendor

    session = object_session(user)
    vendor = session.query(Vendor).filter(Vendor.user_id == user.user_id).first() if session else None
    return vendor_display_name(vendor, user, fallback)


def booking_vendor_name(booking, fallback: str | None = None) -> str | None:
    """vendor_display_name for a booking's vendor, looked up through the
    booking's own session — for code holding only the booking."""
    from sqlalchemy.orm import object_session

    from app.db.models import User, Vendor

    session = object_session(booking)
    if session is None or not booking.vendor_id:
        return fallback
    vendor = session.query(Vendor).filter(Vendor.vendor_id == booking.vendor_id).first()
    user = session.query(User).filter(User.user_id == vendor.user_id).first() if vendor else None
    return vendor_display_name(vendor, user, fallback)
