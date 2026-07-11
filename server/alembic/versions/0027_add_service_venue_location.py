"""add venue location + coordinates to services

Revision ID: 0027_service_venue_location
Revises: 0026_service_negotiable
Create Date: 2026-07-11

Venue services carry a physical location so a booked venue can anchor the
event's venue and supply the GPS coordinates that traveling vendors check in
against. Adds services.location (address string) plus venue_latitude /
venue_longitude. All nullable — only venue-category services populate them,
enforced in the service router; existing rows stay null.
"""
from alembic import op
import sqlalchemy as sa

revision = "0027_service_venue_location"
down_revision = "0026_service_negotiable"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("services", sa.Column("location", sa.String(length=255), nullable=True))
    op.add_column("services", sa.Column("venue_latitude", sa.Float(), nullable=True))
    op.add_column("services", sa.Column("venue_longitude", sa.Float(), nullable=True))


def downgrade():
    op.drop_column("services", "venue_longitude")
    op.drop_column("services", "venue_latitude")
    op.drop_column("services", "location")
