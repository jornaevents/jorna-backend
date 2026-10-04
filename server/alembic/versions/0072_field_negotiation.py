"""Field-by-field contract negotiation (docs/DECISIONS.md #26)

Revision ID: 0072_field_negotiation
Revises: 0071_package_most_popular
Create Date: 2026-10-04

Each side answers the other's changes one field at a time — Accept, Counter
or Keep mine — in strict turns, instead of the whole-proposal model of 0068.

- bookings.negotiation_mode: "fields" on a contract that negotiates this
  way; null keeps the 0068 proposal flow, so contracts already mid-
  negotiation finish as they started.
- bookings.negotiation_round / negotiation_turn: how many sends so far, and
  whose move it is (client | vendor).
- bookings.negotiation_locks / vendors.negotiation_locks: the groups of
  fields the client can't change (prices, event, policies, clauses). The
  vendor's setting is copied onto a contract when it's first sent.
- negotiation_fields: one row per field that isn't plainly agreed.
- negotiation_sends: one row per send, with its answers.

Additive only.
"""
from alembic import op
import sqlalchemy as sa

revision = "0072_field_negotiation"
down_revision = "0071_package_most_popular"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bookings", sa.Column("negotiation_mode", sa.String(10), nullable=True))
    op.add_column("bookings", sa.Column("negotiation_round", sa.Integer(), nullable=True))
    op.add_column("bookings", sa.Column("negotiation_turn", sa.String(10), nullable=True))
    op.add_column("bookings", sa.Column("negotiation_locks", sa.JSON(), nullable=True))
    op.add_column("vendors", sa.Column("negotiation_locks", sa.JSON(), nullable=True))

    op.create_table(
        "negotiation_fields",
        sa.Column("field_id", sa.String(36), primary_key=True),
        sa.Column(
            "booking_id", sa.String(36),
            sa.ForeignKey("bookings.booking_id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("field_key", sa.String(120), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("agreed_value", sa.JSON(), nullable=True),
        sa.Column("proposed_value", sa.JSON(), nullable=True),
        sa.Column("proposed_by", sa.String(10), nullable=True),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(1000), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("booking_id", "field_key", name="uq_negotiation_fields_booking_key"),
    )
    op.create_index("ix_negotiation_fields_booking_id", "negotiation_fields", ["booking_id"])

    op.create_table(
        "negotiation_sends",
        sa.Column("send_id", sa.String(36), primary_key=True),
        sa.Column(
            "booking_id", sa.String(36),
            sa.ForeignKey("bookings.booking_id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("message", sa.String(2000), nullable=True),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_negotiation_sends_booking_id", "negotiation_sends", ["booking_id"])


def downgrade():
    op.drop_index("ix_negotiation_sends_booking_id", table_name="negotiation_sends")
    op.drop_table("negotiation_sends")
    op.drop_index("ix_negotiation_fields_booking_id", table_name="negotiation_fields")
    op.drop_table("negotiation_fields")
    op.drop_column("vendors", "negotiation_locks")
    for col in ("negotiation_locks", "negotiation_turn", "negotiation_round", "negotiation_mode"):
        op.drop_column("bookings", col)
