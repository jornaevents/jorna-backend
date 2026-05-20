"""add instagram_username and instagram_tags to vendors

Revision ID: 0011_add_instagram
Revises: 0010_add_conversations
Create Date: 2026-05-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_add_instagram"
down_revision = "0010_add_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vendors", sa.Column("instagram_username", sa.String(100), nullable=True, unique=True))
    op.add_column("vendors", sa.Column("instagram_tags", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("vendors", "instagram_tags")
    op.drop_column("vendors", "instagram_username")
