"""Seed a staging (or local) database with test vendors and listings.

Staging starts with an empty database, so the marketplace has nothing to book
and end-to-end testing needs a vendor first. This adds a handful of obviously
fake vendors across the main categories, each with a couple of listings and a
Venmo handle so bookings take the manual (off-platform) payment track.

Seeded users have no password and an @example.test email, so nobody can sign
in as them — they exist to be found and booked. Re-running is safe: a vendor
whose email already exists is left alone, apart from filling in a missing
subcategory (see _backfill_subcategory).

    # locally (SQLite)
    venv/bin/python -m scripts.seed_staging
    # against staging, from server/. Through the Postgres service's public URL:
    # the app service's DATABASE_URL is a private host (postgres.railway.internal)
    # that only resolves inside Railway. Same as docs/STAGING.md.
    railway run --service Postgres --environment staging -- sh -c \
      'DATABASE_URL="$DATABASE_PUBLIC_URL" venv/bin/python -m scripts.seed_staging'

Refuses to run against anything that looks like production: a Supabase host,
or a Railway environment other than `staging`.
"""

import os
import sys

from app.config import DATABASE_URL
from app.db.database import SessionLocal
from app.db.models import Service, User, Vendor

# Edison, NJ — a real centre of South Asian weddings, so the seeded vendors sit
# where the product's search and travel-radius logic expects real ones.
_LAT, _LNG, _CITY, _STATE = 40.5187, -74.4121, "Edison", "NJ"

VENDORS = [
    {
        "slug": "dj",
        "name": ("E2E", "Test DJ"),
        "category": "music_entertainment",
        "subcategory": "dj",
        "bio": "Test vendor for end-to-end testing on staging. Not a real business.",
        "services": [
            ("Sangeet DJ set", 1500.0, "event", "Four-hour DJ set with lighting."),
            ("Baraat dhol + DJ", 800.0, "hour", "Mobile sound for the baraat."),
        ],
    },
    {
        "slug": "photo",
        "name": ("E2E", "Test Photographer"),
        "category": "photography",
        "bio": "Test vendor for end-to-end testing on staging. Not a real business.",
        "services": [
            ("Wedding day coverage", 4000.0, "day", "Full-day coverage, two shooters."),
            ("Mehndi coverage", 250.0, "hour", "Candid coverage of the mehndi."),
        ],
    },
    {
        "slug": "catering",
        "name": ("E2E", "Test Caterer"),
        "category": "catering",
        "bio": "Test vendor for end-to-end testing on staging. Not a real business.",
        "services": [
            ("Punjabi buffet", 45.0, "person", "Three mains, two sides, dessert."),
            ("Chaat station", 12.0, "person", "Live chaat counter."),
        ],
    },
    {
        "slug": "venue",
        "name": ("E2E", "Test Banquet Hall"),
        "category": "venue",
        "bio": "Test vendor for end-to-end testing on staging. Not a real business.",
        "services": [
            ("Grand ballroom", 12000.0, "event", "Seats 400, dance floor included."),
        ],
    },
    {
        "slug": "mehndi",
        "name": ("E2E", "Test Mehndi Artist"),
        "category": "beauty",
        "subcategory": "mehndi_artist",
        "bio": "Test vendor for end-to-end testing on staging. Not a real business.",
        "services": [
            ("Bridal mehndi", 600.0, "event", "Full bridal hands and feet."),
            ("Guest mehndi", 150.0, "hour", "Simple designs for guests."),
        ],
    },
]


def _refuse_production() -> None:
    env = os.environ.get("RAILWAY_ENVIRONMENT_NAME")
    if "supabase" in DATABASE_URL:
        sys.exit("Refusing to seed: DATABASE_URL points at Supabase (production).")
    if env and env != "staging":
        sys.exit(f"Refusing to seed: Railway environment is {env!r}, not 'staging'.")
    if not env and not DATABASE_URL.startswith("sqlite"):
        sys.exit("Refusing to seed: not on Railway staging and not a local SQLite database.")


def _backfill_subcategory(db, user: User, v: dict) -> bool:
    """Give an already-seeded vendor the subcategory it was first seeded without.

    The builder's DJ, dhol, mehndi and makeup slots match on subcategory, so a
    music or beauty vendor without one never fills them. That's how staging's
    only DJ read as "No available DJ" (backend #83). Returns whether anything
    changed.
    """
    sub = v.get("subcategory")
    vendor = db.query(Vendor).filter(Vendor.user_id == user.user_id).first()
    if not sub or vendor is None:
        return False
    changed = False
    if vendor.subcategory is None:
        vendor.subcategory = sub
        vendor.specializations = [{"category": v["category"], "subcategory": sub}]
        changed = True
    for service in db.query(Service).filter(
        Service.vendor_id == vendor.vendor_id, Service.subcategory.is_(None)
    ):
        service.subcategory = sub
        changed = True
    return changed


def seed() -> None:
    _refuse_production()
    db = SessionLocal()
    created = skipped = fixed = 0
    try:
        for v in VENDORS:
            email = f"e2e-{v['slug']}@example.test"
            existing = db.query(User).filter(User.email == email).first()
            if existing:
                if _backfill_subcategory(db, existing, v):
                    fixed += 1
                else:
                    skipped += 1
                continue
            user = User(
                username=f"e2e_{v['slug']}",
                email=email,
                password=None,
                f_name=v["name"][0],
                l_name=v["name"][1],
                city=_CITY,
                state=_STATE,
                location=f"{_CITY}, {_STATE}",
                latitude=_LAT,
                longitude=_LNG,
            )
            db.add(user)
            db.flush()
            vendor = Vendor(
                user_id=user.user_id,
                bio=v["bio"],
                category=v["category"],
                subcategory=v.get("subcategory"),
                specializations=[{"category": v["category"], "subcategory": v.get("subcategory")}],
                rating=4.8,
                num_events=0,
                travel_radius_miles=100,
                open_to_long_distance=True,
                payment_method="manual",
                venmo_handle=f"@e2e-{v['slug']}-test",
            )
            db.add(vendor)
            db.flush()
            for name, price, unit, desc in v["services"]:
                db.add(
                    Service(
                        vendor_id=vendor.vendor_id,
                        name=f"E2E {name}",
                        price=price,
                        price_unit=unit,
                        category=v["category"],
                        subcategory=v.get("subcategory"),
                        experience="Test listing — not a real service.",
                        description=desc,
                    )
                )
            created += 1
        db.commit()
    finally:
        db.close()
    print(
        f"Seeded {created} vendor(s); gave {fixed} existing one(s) their "
        f"subcategory; {skipped} already up to date."
    )


if __name__ == "__main__":
    seed()
