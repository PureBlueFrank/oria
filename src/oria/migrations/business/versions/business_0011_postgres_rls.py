"""Enable deny-by-default PostgreSQL tenant row security for business tables."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "business_0011"
down_revision = "business_0010"
branch_labels = None
depends_on = None

_TABLES = (
    "product_snapshots",
    "campaign_rule_snapshot_refs",
    "campaigns",
    "coupon_batches",
    "launch_saga_states",
    "recruitment_publications",
    "enrollments",
    "enrollment_items",
    "enrollment_coupon_links",
    "confirmation_tasks",
    "campaign_approval_bindings",
    "assortment_submissions",
    "assortment_submission_items",
    "selection_decisions",
    "consumer_placements",
    "merchant_notifications",
    "tool_executions",
    "tool_execution_requests",
    "domain_events",
    "audit_events",
    "outbox",
    "merchants",
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in _TABLES:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
        op.execute(
            sa.text(
                f'CREATE POLICY oria_tenant_isolation ON "{table}" '
                "USING (tenant_id = NULLIF(current_setting('oria.tenant_id', true), '')) "
                "WITH CHECK (tenant_id = "
                "NULLIF(current_setting('oria.tenant_id', true), ''))"
            )
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in reversed(_TABLES):
        op.execute(sa.text(f'DROP POLICY oria_tenant_isolation ON "{table}"'))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))
