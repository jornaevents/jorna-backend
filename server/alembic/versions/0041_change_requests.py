"""a paid booking can be asked to move

Revision ID: 0041_change_requests
Revises: 0040_review_service
Create Date: 2026-07-30

Events move. Once a request reached a vendor the details they were told became
frozen — correctly, since that is what they agreed to — but for a paid booking
there was then no route to a new date at all. No cancel button, and the server
refuses to remove a booking once money is involved. Past the 24-hour refund
window the only exits were a dispute, which is adversarial and the wrong shape
for "the venue flooded, we've moved to the 14th", or nothing, which leaves a
vendor turning up on a dead date and escrow releasing seven days after a day
that no longer means anything.

This adds the table behind a change request: the client proposes, each vendor
answers, and only a resolution touches the booking. Escrow does not move on a
proposal.

Additive and nullable throughout — no backfill, nothing existing changes shape.
"""
from alembic import op
import sqlalchemy as sa

revision = "0041_change_requests"
down_revision = "0040_review_service"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "change_requests",
        sa.Column("change_request_id", sa.String(36), primary_key=True),
        sa.Column(
            "booking_id",
            sa.String(36),
            sa.ForeignKey("bookings.booking_id"),
            nullable=False,
        ),
        sa.Column(
            "proposed_by",
            sa.String(36),
            sa.ForeignKey("users.user_id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        # Null means "unchanged" — a proposal may move only the date, only the
        # hours, or both.
        sa.Column("date_iso", sa.String(10), nullable=True),
        sa.Column("date_end", sa.String(10), nullable=True),
        sa.Column("time_start", sa.String(5), nullable=True),
        sa.Column("time_end", sa.String(5), nullable=True),
        # The move waits here when the new dates cost more, until the client
        # agrees to the difference.
        sa.Column("repriced_amount_cents", sa.Integer, nullable=True),
        sa.Column("client_consented_at", sa.DateTime, nullable=True),
        sa.Column("message", sa.Text, nullable=True),
        sa.Column("response_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("resolved_at", sa.DateTime, nullable=True),
    )
    # Read two ways: every request on one booking, and the pending ones a sweep
    # has to expire.
    op.create_index(
        "ix_change_requests_booking_id", "change_requests", ["booking_id"]
    )
    op.create_index("ix_change_requests_status", "change_requests", ["status"])


def downgrade():
    op.drop_index("ix_change_requests_status", table_name="change_requests")
    op.drop_index("ix_change_requests_booking_id", table_name="change_requests")
    op.drop_table("change_requests")
