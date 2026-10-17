"""Leads from the public inquiry form (docs/DECISIONS.md #32)

Revision ID: 0077_lead_inquiry_fields
Revises: 0076_template_service_id
Create Date: 2026-10-17

- leads.event_type: what the event is ("Sangeet"), as the client wrote it.
- leads.guest_count: a rough head count.
- leads.source: where the lead came from — manual (the vendor added it),
  messages ("Add to leads" on a thread) or inquiry_form (POST
  /vendors/{id}/inquiries). Existing leads are manual, or messages when they
  carry a conversation.
"""
from alembic import op
import sqlalchemy as sa

revision = "0077_lead_inquiry_fields"
down_revision = "0076_template_service_id"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("leads", sa.Column("event_type", sa.String(50), nullable=True))
    op.add_column("leads", sa.Column("guest_count", sa.Integer(), nullable=True))
    op.add_column("leads", sa.Column("source", sa.String(20), nullable=False, server_default="manual"))
    op.execute("UPDATE leads SET source = 'messages' WHERE conversation_id IS NOT NULL")
    op.create_index("ix_leads_vendor_source_created", "leads", ["vendor_id", "source", "created_at"])


def downgrade():
    op.drop_index("ix_leads_vendor_source_created", table_name="leads")
    op.drop_column("leads", "source")
    op.drop_column("leads", "guest_count")
    op.drop_column("leads", "event_type")
