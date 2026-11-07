"""Leads keep where the event is and the package asked about (DECISIONS #46)

Revision ID: 0085_lead_location_package
Revises: 0084_email_verification
Create Date: 2026-11-06

- leads.event_location: the event's place, as the client wrote it.
- leads.service_id: the vendor's package the inquiry is about; cleared if
  the package is deleted.
"""
import sqlalchemy as sa

from alembic import op

revision = "0085_lead_location_package"
down_revision = "0084_email_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("event_location", sa.String(255), nullable=True))
    op.add_column("leads", sa.Column("service_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_leads_service_id", "leads", "services", ["service_id"], ["service_id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_leads_service_id", "leads", type_="foreignkey")
    op.drop_column("leads", "service_id")
    op.drop_column("leads", "event_location")
