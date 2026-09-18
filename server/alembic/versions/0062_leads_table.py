"""leads: informal, off-platform prospects a vendor wants to track

Revision ID: 0062_leads_table
Revises: 0061_vendor_contract_defaults
Create Date: 2026-09-19

A Booking requires a committed service_id/date/time/location; an inquiry a
vendor gets off-platform ("DM'd on Instagram, maybe October, no venue yet")
usually has none of that yet. Rather than make half of Booking nullable for
leads too, this is a minimal, separate table — name/contact + free-text
note, nothing else required. Converting a lead creates a real Booking/
contract and sets converted_booking_id; the lead row stays afterward as
the vendor's own CRM history of how that client was won, not deleted.

New table, no relation to any existing column's shape — nothing to backfill.
"""
from alembic import op
import sqlalchemy as sa

revision = "0062_leads_table"
down_revision = "0061_vendor_contract_defaults"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "leads",
        sa.Column("lead_id", sa.String(36), primary_key=True),
        sa.Column(
            "vendor_id", sa.String(36), sa.ForeignKey("vendors.vendor_id"), nullable=False
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("event_date_iso", sa.String(50), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="new"),
        sa.Column(
            "converted_booking_id",
            sa.String(36),
            sa.ForeignKey("bookings.booking_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_leads_vendor_id", "leads", ["vendor_id"])


def downgrade():
    op.drop_index("ix_leads_vendor_id", table_name="leads")
    op.drop_table("leads")
