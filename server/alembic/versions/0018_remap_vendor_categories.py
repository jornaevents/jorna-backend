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


def downgrade():
    for old, new in _DOWNGRADES:
        op.execute(
            f"UPDATE vendors SET category = '{new}' WHERE category = '{old}'"
        )
