"""contract lifecycle: status, tentative hold, decline

Revision ID: 0064_contract_lifecycle
Revises: 0063_package_details
Create Date: 2026-09-26

Phase 2a of the package/contract overhaul. A contract was created APPROVED
and blocked its vendor's date from that moment until someone voided it —
an unsent or ignored link held the date forever. Now:

- bookings.contract_status (draft/sent/viewed/signed/declined/voided,
  check-constrained, null on every non-contract booking) with sent_at,
  viewed_at, hold_expires_at, declined_at, decline_reason, voided_at.
- vendors.contract_hold_days — how long a sent contract holds the date.

Backfill of existing contracts (rows with a contract_token):
- signed_at set                  → 'signed'
- else status 'rejected'         → 'voided' (void was the only way there)
- else                           → 'sent', sent_at = confirmed_at (set at
  creation), and hold_expires_at = now + 7 days. These links are already out
  with clients; giving them a fresh week rather than the age-based expiry
  they'd otherwise have avoids releasing a batch of vendor dates the moment
  this deploys.

Additive only: nothing dropped, renamed or tightened on existing columns.
"""
from datetime import datetime, timedelta, timezone

from alembic import op
import sqlalchemy as sa

revision = "0064_contract_lifecycle"
down_revision = "0063_package_details"
branch_labels = None
depends_on = None

_STATUSES = "('draft', 'sent', 'viewed', 'signed', 'declined', 'voided')"


def upgrade():
    op.add_column("bookings", sa.Column("contract_status", sa.String(20), nullable=True))
    op.create_check_constraint(
        "ck_bookings_contract_status",
        "bookings",
        f"contract_status IS NULL OR contract_status IN {_STATUSES}",
    )
    op.add_column("bookings", sa.Column("sent_at", sa.DateTime(), nullable=True))
    op.add_column("bookings", sa.Column("viewed_at", sa.DateTime(), nullable=True))
    op.add_column("bookings", sa.Column("hold_expires_at", sa.DateTime(), nullable=True))
    op.add_column("bookings", sa.Column("declined_at", sa.DateTime(), nullable=True))
    op.add_column("bookings", sa.Column("decline_reason", sa.String(500), nullable=True))
    op.add_column("bookings", sa.Column("voided_at", sa.DateTime(), nullable=True))
    op.add_column("vendors", sa.Column("contract_hold_days", sa.Integer(), nullable=True))

    # Naive UTC, like every other DateTime column in this schema.
    hold_until = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=7)
    bind = op.get_bind()
    bind.execute(sa.text(
        "UPDATE bookings SET contract_status = 'signed' "
        "WHERE contract_token IS NOT NULL AND signed_at IS NOT NULL"
    ))
    bind.execute(sa.text(
        "UPDATE bookings SET contract_status = 'voided' "
        "WHERE contract_token IS NOT NULL AND signed_at IS NULL AND status = 'rejected'"
    ))
    bind.execute(
        sa.text(
            "UPDATE bookings SET contract_status = 'sent', sent_at = confirmed_at, "
            "hold_expires_at = :hold_until "
            "WHERE contract_token IS NOT NULL AND contract_status IS NULL"
        ),
        {"hold_until": hold_until},
    )


def downgrade():
    op.drop_column("vendors", "contract_hold_days")
    op.drop_column("bookings", "voided_at")
    op.drop_column("bookings", "decline_reason")
    op.drop_column("bookings", "declined_at")
    op.drop_column("bookings", "hold_expires_at")
    op.drop_column("bookings", "viewed_at")
    op.drop_column("bookings", "sent_at")
    op.drop_constraint("ck_bookings_contract_status", "bookings", type_="check")
    op.drop_column("bookings", "contract_status")
