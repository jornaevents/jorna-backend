"""remap vendor categories to new taxonomy

Revision ID: 0018_remap_vendor_categories
Revises: 0017_bundle_group_id
Create Date: 2026-06-09

Maps old flat category values to the new structured taxonomy:
  dj         → music_entertainment
  dhol       → cultural_services
  mehndi     → cultural_services
  decoration → floral_decor
  mua        → beauty
  (all others are already valid new values)
"""
from alembic import op

revision = "0018_remap_vendor_categories"
down_revision = "0017_bundle_group_id"
branch_labels = None
depends_on = None

_UPGRADES = [
    ("dj",         "music_entertainment"),
    ("dhol",       "cultural_services"),
    ("mehndi",     "cultural_services"),
    ("decoration", "floral_decor"),
    ("mua",        "beauty"),
]

# All values that are valid in the new taxonomy — anything not in this set
# after the remaps above gets moved to 'other' so no vendor is left stranded.
_VALID_NEW_CATEGORIES = {
    "venue", "planning", "catering", "bar_beverage", "cakes_desserts",
    "photography", "videography", "music_entertainment", "floral_decor",
    "rentals", "lighting_av", "beauty", "attire", "jewelry", "stationery",
    "transportation", "officiants", "guest_hospitality", "favors_gifts",
    "cultural_services", "post_wedding", "other",
}

_DOWNGRADES = [
    ("music_entertainment", "dj"),
    ("floral_decor",        "decoration"),
    ("beauty",              "mua"),
    # dhol/mehndi both mapped to cultural_services — can't cleanly reverse,
    # so downgrade approximates: cultural_services → dhol
    ("cultural_services",   "dhol"),
]


def upgrade():
    for old, new in _UPGRADES:
        op.execute(
            f"UPDATE vendors SET category = '{new}' WHERE category = '{old}'"
        )

    # Catch-all: any category value not in the new taxonomy → 'other'
    # Builds a SQL NOT IN clause so unrecognised values are never left stranded
    valid_list = ", ".join(f"'{v}'" for v in sorted(_VALID_NEW_CATEGORIES))
    op.execute(
        f"UPDATE vendors SET category = 'other' WHERE category NOT IN ({valid_list})"
    )


def downgrade():
    for old, new in _DOWNGRADES:
        op.execute(
            f"UPDATE vendors SET category = '{new}' WHERE category = '{old}'"
        )
