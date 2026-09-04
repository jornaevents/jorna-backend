"""merge the orphaned vendor-specializations branch back into the main chain

Revision ID: 0052_merge_heads
Revises: 0045_vendor_specializations, 0051_google_busy_cache
Create Date: 2026-09-04

0045_vendor_specializations branched off 0044 in parallel with the real
chain (0046+ all descend from 0045_performer_pricing) and was never
chained back in — `alembic heads` has shown two heads ever since, which
breaks any bare `alembic upgrade head` (including Railway's own
preDeployCommand) once both branches are reachable, and broke CI's
migration-chain check the moment a PR tried to build on the real chain.

Pure merge point: no schema change of its own. See
0045_add_vendor_specializations.py for how its upgrade() was made safe to
run against production, where the column it adds already exists out of
band but the revision itself was never recorded there.
"""
revision = "0052_merge_heads"
down_revision = ("0045_vendor_specializations", "0051_google_busy_cache")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
