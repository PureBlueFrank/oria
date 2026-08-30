"""Add the Business DB execution ledger, facts, audit trail, and outbox."""

import sqlalchemy as sa
from alembic import op

revision = "business_0003"
down_revision = "business_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_executions",
        sa.Column("execution_id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("canonical_args_hash", sa.String(), nullable=False),
        sa.Column("checkpoint_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("receipt_id", sa.String(), nullable=True),
        sa.Column("compensation_status", sa.String(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("execution_id"),
        sa.CheckConstraint("status IN ('reserved', 'executing', 'succeeded', 'failed', 'unknown')"),
        sa.CheckConstraint("attempt_count >= 0"),
        sa.CheckConstraint(
            "(status = 'succeeded' AND receipt_id IS NOT NULL) OR "
            "(status IN ('reserved', 'executing', 'failed') AND receipt_id IS NULL) OR "
            "status = 'unknown'"
        ),
    )
    op.create_index(
        "ix_tool_executions_idempotency",
        "tool_executions",
        ["tenant_id", "tool_name", "idempotency_key"],
        unique=True,
    )
    op.create_table(
        "domain_events",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("aggregate_type", sa.String(), nullable=False),
        sa.Column("aggregate_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
        sa.CheckConstraint("event_version >= 1"),
    )
    op.create_index(
        "ix_domain_events_aggregate_version",
        "domain_events",
        ["tenant_id", "aggregate_type", "aggregate_id", "event_version"],
    )
    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("resource_tenant_id", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("policy_version", sa.String(), nullable=False),
        sa.Column("args_hash", sa.String(), nullable=False),
        sa.Column("result", sa.String(), nullable=False),
        sa.Column("correlation_id", sa.String(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_business_audit_events_tenant_occurred",
        "audit_events",
        ["tenant_id", "occurred_at"],
    )
    op.create_index(
        "ix_business_audit_events_correlation",
        "audit_events",
        ["correlation_id"],
    )
    op.create_table(
        "outbox",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("topic", sa.String(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("event_id"),
        sa.CheckConstraint("attempt_count >= 0"),
    )
    op.create_index(
        "ix_outbox_pending",
        "outbox",
        ["tenant_id", "published_at", "available_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_pending", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index("ix_business_audit_events_correlation", table_name="audit_events")
    op.drop_index("ix_business_audit_events_tenant_occurred", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_domain_events_aggregate_version", table_name="domain_events")
    op.drop_table("domain_events")
    op.drop_index("ix_tool_executions_idempotency", table_name="tool_executions")
    op.drop_table("tool_executions")
