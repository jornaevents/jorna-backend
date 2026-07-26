"""users.password becomes nullable

Revision ID: 0032_password_nullable
Revises: 0031_push_tokens
Create Date: 2026-07-26

Lets an account exist with no password at all, which is what a Google-only
sign-up is: Google supplies an email, a name, and an avatar, and never a
password. Until now /auth/register was the only way to create a user and it
required one, so "Continue with Google" had to drop a new visitor onto a form
asking them to invent a password they would never type again.

NULL now carries meaning: no password has ever been set, so the account is
Google-only and the UI should offer "set a password" rather than "change
password". Code reads it that way — login_user and change_password both guard
on it instead of calling .encode() on None, and /me exposes has_password.

Nothing is rewritten and nothing is backfilled: every existing row keeps its
hash, so this only widens what the column will accept.
"""
from alembic import op
import sqlalchemy as sa

revision = "0032_password_nullable"
down_revision = "0031_push_tokens"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "users",
        "password",
        existing_type=sa.String(length=255),
        nullable=True,
    )


def downgrade():
    # Going back requires every row to have a hash again. Give password-less
    # (Google-only) accounts an unusable one rather than dropping the rows: it is
    # not a valid bcrypt digest, so no password can ever match it, and the owner
    # can still recover the account through the password-reset flow.
    op.execute(
        """
        UPDATE users
        SET password = '!google-only-no-password'
        WHERE password IS NULL
        """
    )
    op.alter_column(
        "users",
        "password",
        existing_type=sa.String(length=255),
        nullable=False,
    )
