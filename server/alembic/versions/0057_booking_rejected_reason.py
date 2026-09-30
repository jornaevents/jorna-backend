"""add booking rejected_reason

status becoming "rejected" covers three different real events (a vendor
declining outright, a vendor withdrawing after already approving, and a
failed reschedule's refund) with nothing recorded to tell them apart. This
adds one nullable column for that reason; existing rows stay null rather
than being backfilled by guessing.

Revision ID: 0057_booking_rejected_reason
Revises: 0056_rebackfill_service_media
Create Date: 2026-09-11 16:37:28.861761

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0057_booking_rejected_reason'
down_revision: Union[str, None] = '0056_rebackfill_service_media'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('bookings', sa.Column('rejected_reason', sa.String(length=30), nullable=True))


def downgrade() -> None:
    op.drop_column('bookings', 'rejected_reason')
