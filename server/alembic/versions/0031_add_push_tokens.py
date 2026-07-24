"""multi-device push tokens

Revision ID: 0031_push_tokens
Revises: 0030_event_venue_coords
Create Date: 2026-07-23

Moves push tokens from the single users.fcm_token column to a push_tokens table,
so one user can have many devices (a phone plus one or more browsers) — the
groundwork for web push. FCM tokens are the same string shape for native and
web, so dispatch stays a single path.

Data-preserving: every existing non-null users.fcm_token is copied into
push_tokens as an "ios" device before the column is dropped, so current mobile
users keep receiving pushes with no app change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0031_push_tokens"
down_revision = "0030_event_venue_coords"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "push_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token", sa.String(length=512), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False, server_default="ios"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )
    op.create_index("ix_push_tokens_user_id", "push_tokens", ["user_id"])

    # Backfill existing device tokens so mobile users don't lose notifications.
    # gen_random_uuid() is available on Postgres 13+ (Railway's Postgres);
    # DISTINCT ON de-dupes should two users somehow share a token string.
    op.execute(
        """
        INSERT INTO push_tokens (id, user_id, token, platform, created_at, last_used_at)
        SELECT gen_random_uuid()::text, user_id, fcm_token, 'ios', now(), now()
        FROM users
        WHERE fcm_token IS NOT NULL AND fcm_token <> ''
        """
    )

    op.drop_column("users", "fcm_token")


def downgrade():
    op.add_column("users", sa.Column("fcm_token", sa.String(length=512), nullable=True))
    # Best-effort restore: put one token back per user (an arbitrary one if a user
    # had several devices — the old column could only hold one anyway).
    op.execute(
        """
        UPDATE users u
        SET fcm_token = pt.token
        FROM (
            SELECT DISTINCT ON (user_id) user_id, token
            FROM push_tokens
            ORDER BY user_id, last_used_at DESC
        ) pt
        WHERE pt.user_id = u.user_id
        """
    )
    op.drop_index("ix_push_tokens_user_id", table_name="push_tokens")
    op.drop_table("push_tokens")
