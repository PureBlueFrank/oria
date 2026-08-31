"""Separate caller request dedupe keys from durable business idempotency."""

import sqlalchemy as sa
from alembic import op

revision = "business_0006"
down_revision = "business_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_tool_executions_tenant_execution",
        "tool_executions",
        ["tenant_id", "execution_id"],
        unique=True,
    )
    op.create_table(
        "tool_execution_requests",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("request_idempotency_key", sa.String(), nullable=False),
        sa.Column("canonical_args_hash", sa.String(), nullable=False),
        sa.Column("execution_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "tool_name", "request_idempotency_key"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "execution_id"],
            ["tool_executions.tenant_id", "tool_executions.execution_id"],
        ),
    )


def downgrade() -> None:
    op.drop_table("tool_execution_requests")
    op.drop_index("uq_tool_executions_tenant_execution", table_name="tool_executions")
