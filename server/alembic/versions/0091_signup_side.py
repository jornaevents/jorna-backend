"""Record which sign-up made each account

Revision ID: 0091_signup_side
Revises: 0090_saved_packages
Create Date: 2026-11-18

- users.signup_side: "vendor" or "client", from the sign-up form (or the side
  chosen before a Google sign-up). An account is still a vendor only when it
  has a vendors row; this says which side it meant to join, so one that signed
  up as a vendor and left before onboarding's first step isn't treated as a
  client. NULL for every existing account: nothing recorded it before now.
"""
import sqlalchemy as sa

from alembic import op

revision = "0091_signup_side"
down_revision = "0090_saved_packages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("signup_side", sa.String(10), nullable=True))
    op.create_check_constraint(
        "ck_users_signup_side",
        "users",
        "signup_side IS NULL OR signup_side IN ('client', 'vendor')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_signup_side", "users", type_="check")
    op.drop_column("users", "signup_side")
