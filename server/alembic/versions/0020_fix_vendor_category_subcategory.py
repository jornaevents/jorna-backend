"""fix vendor category and subcategory mappings

Revision ID: 0020_fix_vendor_category_subcategory
Revises: 0019_vendor_subcategory
Create Date: 2026-06-09

Corrects two issues from migration 0018:
  1. dhol vendors were mapped to cultural_services but belong in
     music_entertainment with subcategory = 'dhol'
  2. mehndi vendors were mapped to cultural_services but belong in
     beauty with subcategory = 'mehndi_artist'

Also back-fills subcategories for vendors whose original category
unambiguously implied one:
  music_entertainment (were all 'dj')  → subcategory = 'dj'
  beauty             (were all 'mua')  → subcategory = 'bridal_makeup'

For cultural_services vendors, tag-based inference is used:
  - tags containing 'dhol'           → music_entertainment / dhol
  - tags containing 'mehndi'/'henna' → beauty / mehndi_artist
  - no matching tags                 → left in cultural_services, subcategory
    stays NULL for vendor to self-update via PATCH /vendors/{vendor_id}
"""
from alembic import op


def upgrade():
    # ── Back-fill unambiguous subcategories ──────────────────────────────

    # All music_entertainment vendors were previously 'dj'
    op.execute("""
        UPDATE vendors
        SET subcategory = 'dj'
        WHERE category = 'music_entertainment'
          AND subcategory IS NULL
    """)

    # All beauty vendors were previously 'mua'
    op.execute("""
        UPDATE vendors
        SET subcategory = 'bridal_makeup'
        WHERE category = 'beauty'
          AND subcategory IS NULL
    """)

    # ── Fix dhol vendors (cultural_services → music_entertainment) ────────
    # Uses tag matching to identify dhol players in cultural_services
    op.execute("""
        UPDATE vendors
        SET category = 'music_entertainment',
            subcategory = 'dhol'
        WHERE category = 'cultural_services'
          AND subcategory IS NULL
          AND vendor_id IN (
              SELECT vt.vendor_id
              FROM vendor_tags vt
              JOIN tags t ON t.tag_id = vt.tag_id
              WHERE LOWER(t.name) LIKE '%dhol%'
          )
    """)

    # ── Fix mehndi vendors (cultural_services → beauty) ───────────────────
    # Uses tag matching to identify mehndi/henna artists in cultural_services
    op.execute("""
        UPDATE vendors
        SET category = 'beauty',
            subcategory = 'mehndi_artist'
        WHERE category = 'cultural_services'
          AND subcategory IS NULL
          AND vendor_id IN (
              SELECT vt.vendor_id
              FROM vendor_tags vt
              JOIN tags t ON t.tag_id = vt.tag_id
              WHERE LOWER(t.name) LIKE '%mehndi%'
                 OR LOWER(t.name) LIKE '%henna%'
          )
    """)

    # Any remaining cultural_services vendors with no matching tags are left
    # with subcategory = NULL. They can self-update via PATCH /vendors/{id}.


def downgrade():
    # Reverse subcategory back-fills (set to NULL)
    op.execute("""
        UPDATE vendors
        SET subcategory = NULL
        WHERE category IN ('music_entertainment', 'beauty', 'cultural_services')
    """)

    # Reverse dhol fix
    op.execute("""
        UPDATE vendors
        SET category = 'cultural_services'
        WHERE category = 'music_entertainment'
          AND subcategory = 'dhol'
    """)

    # Reverse mehndi fix
    op.execute("""
        UPDATE vendors
        SET category = 'cultural_services'
        WHERE category = 'beauty'
          AND subcategory = 'mehndi_artist'
    """)
