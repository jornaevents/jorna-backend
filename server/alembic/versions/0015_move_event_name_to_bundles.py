"""move event_name from bookings to bundles

Revision ID: 0015_move_event_name
Revises: 0014_negotiation_flags
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "0015_move_event_name"
down_revision = "0014_negotiation_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add event_name to bundles
    op.add_column("bundles", sa.Column("event_name", sa.String(255), nullable=True))

    # Copy event_name from each bundle's first booking
    conn = op.get_bind()
    conn.execute(text("""
        UPDATE bundles
        SET event_name = (
            SELECT event_name FROM bookings
            WHERE bookings.bundle_id = bundles.bundle_id
            LIMIT 1
        )
        WHERE event_name IS NULL
    """))

    # Drop event_name from bookings
    op.drop_column("bookings", "event_name")


def downgrade() -> None:
    op.add_column("bookings", sa.Column("event_name", sa.String(255), nullable=True))
    op.drop_column("bundles", "event_name")
