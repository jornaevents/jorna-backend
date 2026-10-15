"""A contract template can be a package's own (docs/DECISIONS.md #31)

Revision ID: 0076_template_service_id
Revises: 0075_vendor_seen_at
Create Date: 2026-10-15

- contract_templates.service_id: set when the template is the vendor's
  edited copy of a package's template. Every package has a template built
  from its details until the vendor edits one (the web builds those; nothing
  is stored); saving an edit stores it here, linked to the package. At most
  one per package. A deleted package leaves the copy as an ordinary template.

Additive only; nothing to backfill.
"""
from alembic import op
import sqlalchemy as sa

revision = "0076_template_service_id"
down_revision = "0075_vendor_seen_at"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "contract_templates",
        sa.Column(
            "service_id", sa.String(36),
            sa.ForeignKey("services.service_id", ondelete="SET NULL", name="fk_contract_templates_service"),
            nullable=True,
        ),
    )
    # NULLs are distinct, so ordinary templates aren't limited by this.
    op.create_unique_constraint(
        "uq_contract_templates_vendor_service", "contract_templates", ["vendor_id", "service_id"],
    )


def downgrade():
    op.drop_constraint("uq_contract_templates_vendor_service", "contract_templates", type_="unique")
    op.drop_constraint("fk_contract_templates_service", "contract_templates", type_="foreignkey")
    op.drop_column("contract_templates", "service_id")
