"""Enable deny-by-default PostgreSQL tenant row security for platform tables."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "platform_0009"
down_revision = "platform_0008"
branch_labels = None
depends_on = None

_TABLES = (
    "documents",
    "document_versions",
    "ingestion_runs",
    "rule_snapshot_cache",
    "read_policy",
    "audit_events",
    "outbox",
    "approvals",
    "approval_binding_invalidations",
    "external_waits",
    "integration_event_inbox",
    "memory_items",
    "memory_embeddings",
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
