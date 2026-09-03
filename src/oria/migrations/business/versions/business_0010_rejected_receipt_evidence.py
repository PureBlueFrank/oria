"""Retain rejected external receipt identity and its redacted summary hash."""

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "business_0010"
down_revision = "business_0009"
branch_labels = None
depends_on = None


def _tool_executions(*, include_summary: bool, receipt_check: str) -> sa.Table:
    metadata = sa.MetaData()
    columns: list[Any] = [
        sa.Column("execution_id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("canonical_args_hash", sa.String(), nullable=False),
        sa.Column("checkpoint_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("receipt_id", sa.String(), nullable=True),
    ]
    if include_summary:
        columns.append(sa.Column("receipt_summary_hash", sa.String(), nullable=True))
    columns.extend(
        [
            sa.Column("compensation_status", sa.String(), nullable=True),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("execution_id"),
            sa.CheckConstraint(
                "status IN ('reserved', 'executing', 'succeeded', 'failed', 'unknown')",
                name="ck_tool_executions_status",
            ),
            sa.CheckConstraint(
                "attempt_count >= 0",
                name="ck_tool_executions_attempt_count",
            ),
            sa.CheckConstraint(receipt_check, name="ck_tool_executions_receipt_evidence"),
        ]
    )
    table = sa.Table("tool_executions", metadata, *columns)
    sa.Index(
        "ix_tool_executions_idempotency",
        table.c.tenant_id,
        table.c.tool_name,
        table.c.idempotency_key,
        unique=True,
    )
    sa.Index(
        "uq_tool_executions_tenant_execution",
        table.c.tenant_id,
        table.c.execution_id,
        unique=True,
    )
    return table


def upgrade() -> None:
    legacy = _tool_executions(
        include_summary=False,
        receipt_check=(
            "(status = 'succeeded' AND receipt_id IS NOT NULL) OR "
            "(status IN ('reserved', 'executing', 'failed') AND receipt_id IS NULL) OR "
            "status = 'unknown'"
        ),
    )
    with op.batch_alter_table(
        "tool_executions",
        recreate="always",
        copy_from=legacy,
    ) as batch:
        batch.drop_constraint("ck_tool_executions_receipt_evidence", type_="check")
        batch.add_column(sa.Column("receipt_summary_hash", sa.String(), nullable=True))
        batch.create_check_constraint(
            "ck_tool_executions_receipt_evidence",
            "(status = 'succeeded' AND receipt_id IS NOT NULL) OR "
            "(status IN ('reserved', 'executing') AND receipt_id IS NULL AND "
            "receipt_summary_hash IS NULL) OR status IN ('failed', 'unknown')",
        )
        batch.create_check_constraint(
            "ck_tool_executions_receipt_summary_binding",
            "receipt_summary_hash IS NULL OR receipt_id IS NOT NULL",
        )


def downgrade() -> None:
    op.execute(
        "UPDATE tool_executions SET receipt_id = NULL, receipt_summary_hash = NULL "
        "WHERE status = 'failed'"
    )
    current = _tool_executions(
        include_summary=True,
        receipt_check=(
            "(status = 'succeeded' AND receipt_id IS NOT NULL) OR "
            "(status IN ('reserved', 'executing') AND receipt_id IS NULL AND "
            "receipt_summary_hash IS NULL) OR status IN ('failed', 'unknown')"
        ),
    )
    with op.batch_alter_table(
        "tool_executions",
        recreate="always",
        copy_from=current,
    ) as batch:
        batch.drop_constraint("ck_tool_executions_receipt_evidence", type_="check")
        batch.drop_column("receipt_summary_hash")
        batch.create_check_constraint(
            "ck_tool_executions_receipt_evidence",
            "(status = 'succeeded' AND receipt_id IS NOT NULL) OR "
            "(status IN ('reserved', 'executing', 'failed') AND receipt_id IS NULL) OR "
            "status = 'unknown'",
        )
