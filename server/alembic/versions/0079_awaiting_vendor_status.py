"""Allow contract_status 'awaiting_vendor' (docs/DECISIONS.md #34)

Revision ID: 0079_awaiting_vendor_status
Revises: 0078_vendor_countersign
Create Date: 2026-10-20

0078 added the state between the client's signature and the vendor's, but
not to ck_bookings_contract_status (0064), so every client signature failed
on Postgres. SQLite, which the tests run on, never had the constraint.
tests/test_contract_status_constraint.py now checks the list against what
the code writes.
"""
from alembic import op

revision = "0079_awaiting_vendor_status"
down_revision = "0078_vendor_countersign"
branch_labels = None
depends_on = None

STATUSES = ("draft", "sent", "viewed", "awaiting_vendor", "signed", "declined", "voided")
_OLD = ("draft", "sent", "viewed", "signed", "declined", "voided")


def _replace(statuses):
    op.drop_constraint("ck_bookings_contract_status", "bookings", type_="check")
    listed = ", ".join(f"'{s}'" for s in statuses)
    op.create_check_constraint(
        "ck_bookings_contract_status", "bookings", f"contract_status IS NULL OR contract_status IN ({listed})",
    )


def upgrade():
    _replace(STATUSES)


def downgrade():
    # A contract still waiting on its vendor goes back to the client's
    # signature alone counting, as before 0078.
    op.execute("UPDATE bookings SET contract_status = 'signed', signed_at = client_signed_at "
               "WHERE contract_status = 'awaiting_vendor'")
    _replace(_OLD)
