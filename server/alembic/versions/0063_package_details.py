"""package details: status, inclusions, add-ons, per-package terms

Revision ID: 0063_package_details
Revises: 0062_leads_table
Create Date: 2026-09-23

Phase 1 of the package/contract overhaul. A package was one price and a
free-text description; this adds what a production listing needs:

- services.status (active/hidden/archived, NOT NULL, server default
  'active' so every existing row stays exactly as listed as it is today),
  with a check constraint mirroring the model's.
- services.included_hours / inclusions / add_ons.
- services.deposit_percent / cancellation_window_hours /
  overtime_rate_cents — optional per-package overrides of the vendor's
  default_* contract terms (0061).
- services.sort_order.
- vendors.years_experience, backfilled (Postgres only) from the leading
  number in the vendor's existing services.experience text ("9 years" → 9),
  taking the largest. Rows whose text doesn't start with a 1–2 digit number
  are left null rather than guessed. Capped at two digits so a stray
  "20240 years" can't overflow the cast and fail the deploy.

Additive only: no column is dropped, renamed or tightened.
"""
from alembic import op
import sqlalchemy as sa

revision = "0063_package_details"
down_revision = "0062_leads_table"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "services",
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
    )
    op.create_check_constraint(
        "ck_services_status", "services", "status IN ('active', 'hidden', 'archived')"
    )
    op.add_column("services", sa.Column("included_hours", sa.Float(), nullable=True))
    op.add_column("services", sa.Column("inclusions", sa.JSON(), nullable=True))
    op.add_column("services", sa.Column("add_ons", sa.JSON(), nullable=True))
    op.add_column("services", sa.Column("deposit_percent", sa.Integer(), nullable=True))
    op.add_column(
        "services", sa.Column("cancellation_window_hours", sa.Integer(), nullable=True)
    )
    op.add_column("services", sa.Column("overtime_rate_cents", sa.Integer(), nullable=True))
    op.add_column("services", sa.Column("sort_order", sa.Integer(), nullable=True))
    op.add_column("vendors", sa.Column("years_experience", sa.Integer(), nullable=True))

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            UPDATE vendors v
            SET years_experience = sub.years
            FROM (
                SELECT vendor_id,
                       MAX(CAST(substring(experience FROM '^\\s*(\\d{1,2})(?:\\D|$)') AS INTEGER)) AS years
                FROM services
                WHERE experience ~ '^\\s*\\d{1,2}(\\D|$)'
                GROUP BY vendor_id
            ) sub
            WHERE v.vendor_id = sub.vendor_id
            """
        )


def downgrade():
    op.drop_column("vendors", "years_experience")
    op.drop_column("services", "sort_order")
    op.drop_column("services", "overtime_rate_cents")
    op.drop_column("services", "cancellation_window_hours")
    op.drop_column("services", "deposit_percent")
    op.drop_column("services", "add_ons")
    op.drop_column("services", "inclusions")
    op.drop_column("services", "included_hours")
    op.drop_constraint("ck_services_status", "services", type_="check")
    op.drop_column("services", "status")
