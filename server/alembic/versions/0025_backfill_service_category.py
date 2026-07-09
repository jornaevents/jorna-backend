"""backfill service.category/subcategory from the owning vendor

Revision ID: 0025_service_category_backfill
Revises: 0024_unique_live_booking
Create Date: 2026-07-09

Service-first bundle matching keys off Service.category, but that column was
nullable/optional until now, so historical services may have no category. This
one-off data migration backfills each service's category (and subcategory) from
its owning vendor wherever it's missing, so no service is invisible to slot
matching. Going forward the API guarantees a category on create (defaulting to
the vendor's own category when the client omits it).

Data-only: no schema change, and the column stays nullable.
"""
from alembic import op

revision = "0025_service_category_backfill"
down_revision = "0024_unique_live_booking"
branch_labels = None
depends_on = None


def upgrade():
    # 1) Backfill category from the owning vendor where the service has none.
    op.execute(
        """
        UPDATE services
        SET category = (
            SELECT v.category FROM vendors v WHERE v.vendor_id = services.vendor_id
        )
        WHERE (category IS NULL OR category = '')
          AND EXISTS (SELECT 1 FROM vendors v WHERE v.vendor_id = services.vendor_id)
        """
    )

    # 2) Backfill subcategory from the vendor, but only where the service now
    #    shares the vendor's category (so the inherited subcategory is valid for
    #    it) and the service has no subcategory of its own.
    op.execute(
        """
        UPDATE services
        SET subcategory = (
            SELECT v.subcategory FROM vendors v WHERE v.vendor_id = services.vendor_id
        )
        WHERE (subcategory IS NULL OR subcategory = '')
          AND EXISTS (
              SELECT 1 FROM vendors v
              WHERE v.vendor_id = services.vendor_id
                AND v.category = services.category
                AND v.subcategory IS NOT NULL
                AND v.subcategory <> ''
          )
        """
    )


def downgrade():
    # Backfilled data can't be distinguished from vendor-set values afterward,
    # so this migration is not reversible. No-op.
    pass
