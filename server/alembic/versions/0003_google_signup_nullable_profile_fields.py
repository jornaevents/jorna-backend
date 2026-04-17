"""make age/location/gender/language nullable for Google sign-up users

Revision ID: 0003_google_signup
Revises: 36189298db33
Create Date: 2026-04-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_google_signup"
down_revision = "36189298db33"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("age", existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column("location", existing_type=sa.String(100), nullable=True)
        batch_op.alter_column("gender", existing_type=sa.String(50), nullable=True)
        batch_op.alter_column("language", existing_type=sa.String(50), nullable=True)


def downgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("language", existing_type=sa.String(50), nullable=False)
        batch_op.alter_column("gender", existing_type=sa.String(50), nullable=False)
        batch_op.alter_column("location", existing_type=sa.String(100), nullable=False)
        batch_op.alter_column("age", existing_type=sa.Integer(), nullable=False)
