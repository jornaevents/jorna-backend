"""add content_reports and user_blocks tables

Revision ID: 0023_reports_blocks
Revises: 0022_service_subcategory
Create Date: 2026-07-03
"""
from alembic import op
import sqlalchemy as sa

revision = "0023_reports_blocks"
down_revision = "0022_service_subcategory"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "content_reports",
        sa.Column("report_id", sa.String(36), primary_key=True),
        sa.Column("reporter_user_id", sa.String(36), sa.ForeignKey("users.user_id"), nullable=False, index=True),
        sa.Column("target_type", sa.String(20), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False, index=True),
        sa.Column("reason", sa.String(50), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "user_blocks",
        sa.Column("block_id", sa.String(36), primary_key=True),
        sa.Column("blocker_user_id", sa.String(36), sa.ForeignKey("users.user_id"), nullable=False, index=True),
        sa.Column("blocked_user_id", sa.String(36), sa.ForeignKey("users.user_id"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("blocker_user_id", "blocked_user_id", name="uq_user_block"),
    )


def downgrade():
    op.drop_table("user_blocks")
    op.drop_table("content_reports")
