"""add a per-performer pricing unit and performer_count

Revision ID: 0045_performer_pricing
Revises: 0044_booking_client_note
Create Date: 2026-09-02

Entertainment vendors (dance troupes, bands) often charge by how many
performers a client asks them to provide, not a flat event rate or a
per-guest one. This adds "performer" as a fifth price_unit alongside the
existing person/hour/day/event, and a performer_count column on bookings
mirroring guest_count — same "can't total the booking until the quantity is
known" mechanics booking_service/plan_readiness already apply to a
per-person service.

Nullable/additive on the bookings side: existing rows stay null. The
services.price_unit check constraint has to be dropped and recreated rather
than widened in place, same as it was first added in 0039.
"""
from alembic import op
import sqlalchemy as sa

revision = "0045_performer_pricing"
down_revision = "0044_booking_client_note"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("services") as batch:
        batch.drop_constraint("ck_services_price_unit", type_="check")
        batch.create_check_constraint(
            "ck_services_price_unit",
            "price_unit IS NULL OR price_unit IN ('person', 'hour', 'day', 'event', 'performer')",
        )

    op.add_column("bookings", sa.Column("performer_count", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("bookings", "performer_count")

    # A service actually priced 'performer' would violate the narrower
    # constraint below — same trade-off 0039's downgrade makes: this reverts
    # the rule, not any row that came to depend on the wider one.
    with op.batch_alter_table("services") as batch:
        batch.drop_constraint("ck_services_price_unit", type_="check")
        batch.create_check_constraint(
            "ck_services_price_unit",
            "price_unit IS NULL OR price_unit IN ('person', 'hour', 'day', 'event')",
        )
