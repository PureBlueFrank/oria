"""Replace the provisional launch saga states with the fixed T04 state machine."""

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "business_0004"
down_revision = "business_0003"
branch_labels = None
depends_on = None

_CURRENT_STATES = (
    "planned",
    "coupon_materialized",
    "recruitment_published",
    "completed",
    "compensation_pending",
    "reconciliation_required",
    "failed",
)
_LEGACY_STATES = ("pending", "running", "completed", "failed", "unknown")


def _identity_columns() -> tuple[sa.Column[Any], ...]:
    return (
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("launch_saga_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def _create_replacement(table_name: str, states: tuple[str, ...]) -> None:
    quoted_states = ", ".join(f"'{state}'" for state in states)
    op.create_table(
        table_name,
        *_identity_columns(),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("checkpoint", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "launch_saga_id"),
        sa.UniqueConstraint("tenant_id", "launch_saga_id", "version"),
        sa.UniqueConstraint("tenant_id", "campaign_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.campaign_id"],
        ),
        sa.CheckConstraint(f"status IN ({quoted_states})"),
    )


def _replace_table(*, statuses_sql: str, states: tuple[str, ...]) -> None:
    replacement = "launch_saga_states_t04"
    _create_replacement(replacement, states)
    op.execute(
        sa.text(
            f"INSERT INTO {replacement} "
            "(tenant_id, launch_saga_id, version, created_at, updated_at, campaign_id, "
            "status, checkpoint) SELECT tenant_id, launch_saga_id, version, created_at, "
            f"updated_at, campaign_id, {statuses_sql}, checkpoint FROM launch_saga_states"
        )
    )
    op.drop_table("launch_saga_states")
    op.rename_table(replacement, "launch_saga_states")


def upgrade() -> None:
    _replace_table(
        statuses_sql=(
            "CASE status WHEN 'pending' THEN 'planned' WHEN 'running' THEN "
            "'coupon_materialized' WHEN 'unknown' THEN 'reconciliation_required' ELSE status END"
        ),
        states=_CURRENT_STATES,
    )


def downgrade() -> None:
    _replace_table(
        statuses_sql=(
            "CASE status WHEN 'planned' THEN 'pending' WHEN 'coupon_materialized' THEN "
            "'running' WHEN 'recruitment_published' THEN 'running' WHEN "
            "'compensation_pending' THEN 'failed' WHEN 'reconciliation_required' THEN "
            "'unknown' ELSE status END"
        ),
        states=_LEGACY_STATES,
    )
