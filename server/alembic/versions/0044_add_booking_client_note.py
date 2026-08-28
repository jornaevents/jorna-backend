"""add client_note to bookings

Revision ID: 0044_booking_client_note
Revises: 0043_typed_service_media
Create Date: 2026-08-28

The frontend has sent this on POST /bookings for a while ("Anything the
vendor should know?") but nothing stored it, so a vendor never saw what a
client wrote there. Nullable/additive: existing rows stay null.
"""
from alembic import op
import sqlalchemy as sa

revision = "0044_booking_client_note"
down_revision = "0043_typed_service_media"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bookings", sa.Column("client_note", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("bookings", "client_note")
