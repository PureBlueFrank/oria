"""Add bound approvals, external waits, and the sanitized integration inbox."""

import sqlalchemy as sa
from alembic import op

revision = "platform_0005"
down_revision = "platform_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("approval_id", sa.String(), nullable=False),
        sa.Column("approval_action", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("canonical_args_hash", sa.String(), nullable=False),
        sa.Column("checkpoint_id", sa.String(), nullable=False),
        sa.Column("policy_version", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("requester", sa.String(), nullable=False),
        sa.Column("decider", sa.String(), nullable=True),
        sa.Column("decision", sa.String(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "approval_id"),
        sa.CheckConstraint("approval_action IN ('launch_approval', 'consumer_publish_approval')"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'invalidated')"
        ),
        sa.CheckConstraint("decision IS NULL OR decision IN ('approve', 'reject')"),
        sa.CheckConstraint("decider IS NULL OR requester <> decider"),
    )
    op.create_index(
        "ix_approvals_tenant_status_expires",
        "approvals",
        ["tenant_id", "status", "expires_at"],
    )
    op.create_table(
        "external_waits",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("wait_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("checkpoint_id", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timeout_action", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "wait_id"),
        sa.CheckConstraint(
            "event_type IN ('merchant.enrollment_upserted', 'enrollment.window_closed', "
            "'selection.decision_recorded', 'selection.completed')"
        ),
        sa.CheckConstraint("expected_version >= 1"),
        sa.CheckConstraint("timeout_action IN ('queue', 'fail', 'cancel')"),
        sa.CheckConstraint("status IN ('waiting', 'matched', 'expired', 'cancelled')"),
    )
    op.create_index(
        "ix_external_waits_match",
        "external_waits",
        ["tenant_id", "event_type", "resource_type", "resource_id", "status"],
    )
    op.create_table(
        "integration_event_inbox",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("adapter_id", sa.String(), nullable=False),
        sa.Column("source_event_id", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("resource_version", sa.Integer(), nullable=False),
        sa.Column("signature_subject", sa.String(), nullable=False),
        sa.Column("redacted_payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(), nullable=False),
        sa.Column("processing_status", sa.String(), nullable=False),
        sa.Column("wait_id", sa.String(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "adapter_id", "source_event_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "wait_id"],
            ["external_waits.tenant_id", "external_waits.wait_id"],
        ),
        sa.CheckConstraint("schema_version = 1"),
        sa.CheckConstraint("resource_version >= 1"),
        sa.CheckConstraint(
            "processing_status IN ('matched', 'consumed', 'unauthorized', 'no_wait', "
            "'type_mismatch', 'resource_mismatch', 'stale', 'out_of_order', 'wait_expired')"
        ),
    )
    op.create_index(
        "ix_integration_event_inbox_status",
        "integration_event_inbox",
        ["tenant_id", "processing_status", "received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_integration_event_inbox_status", table_name="integration_event_inbox")
    op.drop_table("integration_event_inbox")
    op.drop_index("ix_external_waits_match", table_name="external_waits")
    op.drop_table("external_waits")
    op.drop_index("ix_approvals_tenant_status_expires", table_name="approvals")
    op.drop_table("approvals")
