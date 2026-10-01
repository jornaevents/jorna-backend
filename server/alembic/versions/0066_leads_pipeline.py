"""leads pipeline: booking created_at, archiving, leads from conversations, mark unread

Revision ID: 0066_leads_pipeline
Revises: 0065_contract_proposals
Create Date: 2026-10-01

The vendor web app's Leads page (redesign step 2, docs/DECISIONS.md #20):

- bookings.created_at — when a request or contract was made. Bookings never
  recorded it, so "new this week" and "waiting since" had nothing to read.
  Null on rows from before this; no backfill, since nothing on a booking
  says when it was made.
- bookings.vendor_archived_at, leads.archived_at — the vendor hid it from
  their active list. Not a status: archiving doesn't decline or void
  anything, and unarchiving puts it back as it was.
- leads.user_id, leads.conversation_id — a lead made from a Messages thread
  ("Add to leads"), so the lead knows who it is and where the talk is.
- conversation_members.marked_unread_at — "Mark as unread", per person.

Additive only.
"""
from alembic import op
import sqlalchemy as sa

revision = "0066_leads_pipeline"
down_revision = "0065_contract_proposals"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bookings", sa.Column("created_at", sa.DateTime(), nullable=True))
    op.add_column("bookings", sa.Column("vendor_archived_at", sa.DateTime(), nullable=True))
    op.add_column("leads", sa.Column("archived_at", sa.DateTime(), nullable=True))
    op.add_column(
        "leads",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id"), nullable=True),
    )
    op.add_column(
        "leads",
        sa.Column(
            "conversation_id", sa.String(36),
            sa.ForeignKey("conversations.conversation_id"), nullable=True,
        ),
    )
    op.create_index("ix_leads_conversation_id", "leads", ["conversation_id"])
    op.add_column("conversation_members", sa.Column("marked_unread_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("conversation_members", "marked_unread_at")
    op.drop_index("ix_leads_conversation_id", table_name="leads")
    op.drop_column("leads", "conversation_id")
    op.drop_column("leads", "user_id")
    op.drop_column("leads", "archived_at")
    op.drop_column("bookings", "vendor_archived_at")
    op.drop_column("bookings", "created_at")
