"""Add inactive and cancelled states to deterministic confirmation chains."""

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "business_0007"
down_revision = "business_0006"
branch_labels = None
depends_on = None

_CURRENT_STATES = ("waiting", "pending", "confirmed", "rejected", "timed_out", "cancelled")
_LEGACY_STATES = ("pending", "confirmed", "rejected", "timed_out")


def _identity_columns() -> tuple[sa.Column[Any], ...]:
    return (
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("confirmation_task_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def _create_replacement(table_name: str, states: tuple[str, ...]) -> None:
    quoted_states = ", ".join(f"'{state}'" for state in states)
    op.create_table(
        table_name,
        *_identity_columns(),
        sa.Column("enrollment_item_id", sa.String(), nullable=False),
        sa.Column("subject_type", sa.String(), nullable=False),
        sa.Column("subject_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timeout_action", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "confirmation_task_id"),
        sa.UniqueConstraint("tenant_id", "confirmation_task_id", "version"),
        sa.UniqueConstraint("tenant_id", "enrollment_item_id", "sequence"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "enrollment_item_id"],
            ["enrollment_items.tenant_id", "enrollment_items.enrollment_item_id"],
        ),
        sa.CheckConstraint("subject_type IN ('merchant', 'sales', 'sales_manager')"),
        sa.CheckConstraint("timeout_action IN ('reject', 'escalate', 'explicit_auto_confirm')"),
        sa.CheckConstraint(f"status IN ({quoted_states})"),
    )


def _replace_table(*, status_sql: str, states: tuple[str, ...]) -> None:
    replacement = "confirmation_tasks_t05"
    _create_replacement(replacement, states)
    op.execute(
        sa.text(
            f"INSERT INTO {replacement} (tenant_id, confirmation_task_id, version, "
            "created_at, updated_at, enrollment_item_id, subject_type, subject_id, sequence, "
            "due_at, timeout_action, status) SELECT tenant_id, confirmation_task_id, version, "
            "created_at, updated_at, enrollment_item_id, subject_type, subject_id, sequence, "
            f"due_at, timeout_action, {status_sql} FROM confirmation_tasks"
        )
    )
    op.drop_table("confirmation_tasks")
    op.rename_table(replacement, "confirmation_tasks")


def upgrade() -> None:
    _replace_table(status_sql="status", states=_CURRENT_STATES)


def downgrade() -> None:
    _replace_table(
        status_sql="CASE status WHEN 'waiting' THEN 'pending' WHEN 'cancelled' THEN 'rejected' "
        "ELSE status END",
        states=_LEGACY_STATES,
    )
