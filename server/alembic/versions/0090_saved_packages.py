"""Clients save packages, not vendors

Revision ID: 0090_saved_packages
Revises: 0089_registry
Create Date: 2026-11-15

- saved_packages: the packages a client has hearted in Explore, to compare side
  by side. One row per client and package.
- Each existing saved vendor becomes a saved row for each of that vendor's
  active packages, so nobody's shortlist empties out under them. saved_vendors
  itself is left in place for now (its endpoints still answer) and can be
  dropped once nothing reads it.
"""
import uuid
from datetime import datetime

import sqlalchemy as sa

from alembic import op

revision = "0090_saved_packages"
down_revision = "0089_registry"
branch_labels = None
depends_on = None

# Same cap as the router's, so a carried-over shortlist is one the app accepts.
MAX_SAVED = 50


def upgrade() -> None:
    op.create_table(
        "saved_packages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.String(36), sa.ForeignKey("services.service_id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "service_id", name="uq_saved_packages_user_service"),
    )
    op.create_index("ix_saved_packages_user_id", "saved_packages", ["user_id"])

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT sv.user_id, s.service_id, sv.created_at FROM saved_vendors sv "
            "JOIN services s ON s.vendor_id = sv.vendor_id AND s.status = 'active' "
            "ORDER BY sv.user_id, sv.created_at DESC, s.sort_order, s.name"
        )
    ).fetchall()
    per_user: dict[str, int] = {}
    inserts = []
    for user_id, service_id, created_at in rows:
        if per_user.get(user_id, 0) >= MAX_SAVED:
            continue
        per_user[user_id] = per_user.get(user_id, 0) + 1
        inserts.append(
            {"id": str(uuid.uuid4()), "user_id": user_id, "service_id": service_id,
             "created_at": created_at or datetime.utcnow()}
        )
    if inserts:
        table = sa.table(
            "saved_packages",
            sa.column("id", sa.String), sa.column("user_id", sa.String),
            sa.column("service_id", sa.String), sa.column("created_at", sa.DateTime),
        )
        op.bulk_insert(table, inserts)


def downgrade() -> None:
    op.drop_index("ix_saved_packages_user_id", table_name="saved_packages")
    op.drop_table("saved_packages")
