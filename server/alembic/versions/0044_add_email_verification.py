"""add email verification (users.email_verified + email_verification_tokens)

Revision ID: 0044_add_email_verification
Revises: 0043_typed_service_media
Create Date: 2026-08-26

Adds users.email_verified (bool) and users.email_verification_sent_at
(nullable timestamp), plus a single-use token table mirroring
password_reset_tokens. Every existing row is backfilled to verified in this
same migration — the column defaults false going forward (new registrations
start unverified), but a deploy must not lock out everyone who already has an
account, since nobody was ever asked to verify before this existed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0044_add_email_verification'
down_revision: Union[str, None] = '0043_typed_service_media'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('email_verified', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.add_column(
        'users',
        sa.Column('email_verification_sent_at', sa.DateTime(), nullable=True),
    )
    # Backfill: everyone who already has an account was never asked to prove
    # their email, so treat them as verified rather than retroactively locking
    # them out of money/messaging actions.
    op.execute('UPDATE users SET email_verified = true')

    op.create_table(
        'email_verification_tokens',
        sa.Column('token_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id']),
        sa.PrimaryKeyConstraint('token_id'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index(
        op.f('ix_email_verification_tokens_user_id'),
        'email_verification_tokens', ['user_id'], unique=False,
    )
    op.create_index(
        op.f('ix_email_verification_tokens_token_hash'),
        'email_verification_tokens', ['token_hash'], unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_email_verification_tokens_token_hash'), table_name='email_verification_tokens')
    op.drop_index(op.f('ix_email_verification_tokens_user_id'), table_name='email_verification_tokens')
    op.drop_table('email_verification_tokens')
    op.drop_column('users', 'email_verification_sent_at')
    op.drop_column('users', 'email_verified')
