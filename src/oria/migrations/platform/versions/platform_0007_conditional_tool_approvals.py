"""Allow persisted approvals for conditional assortment and notification tools."""

import sqlalchemy as sa
from alembic import op

revision = "platform_0007"
down_revision = "platform_0006"
branch_labels = None
depends_on = None

_COLUMNS = (
    "tenant_id, approval_id, approval_action, tool_name, canonical_args_hash, "
    "checkpoint_id, policy_version, expires_at, status, requester, decider, decision, "
    "reason, created_at, updated_at, decided_at, campaign_id, enrollment_version, "
    "link_version, selection_version, selection_hash, rule_snapshot_hash"
)
_BASE_ACTIONS = ("launch_approval", "consumer_publish_approval")
_T06_ACTIONS = (
    *_BASE_ACTIONS,
    "assortment_submission_approval",
    "merchant_notification_approval",
)


def _create_approvals(actions: tuple[str, ...]) -> None:
    allowed = ", ".join(f"'{action}'" for action in actions)
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
        sa.Column("campaign_id", sa.String(), nullable=True),
        sa.Column("enrollment_version", sa.Integer(), nullable=True),
        sa.Column("link_version", sa.Integer(), nullable=True),
        sa.Column("selection_version", sa.String(), nullable=True),
        sa.Column("selection_hash", sa.String(), nullable=True),
        sa.Column("rule_snapshot_hash", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "approval_id"),
        sa.CheckConstraint(f"approval_action IN ({allowed})"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'invalidated')"
        ),
        sa.CheckConstraint("decision IS NULL OR decision IN ('approve', 'reject')"),
        sa.CheckConstraint("decider IS NULL OR requester <> decider"),
    )


def _rebuild(actions: tuple[str, ...]) -> None:
    op.rename_table("approvals", "approvals_before_t06")
    _create_approvals(actions)
    op.execute(f"INSERT INTO approvals ({_COLUMNS}) SELECT {_COLUMNS} FROM approvals_before_t06")
    op.drop_table("approvals_before_t06")
    op.create_index(
        "ix_approvals_tenant_status_expires",
        "approvals",
        ["tenant_id", "status", "expires_at"],
    )
    op.create_index(
        "ix_approvals_tenant_campaign_status",
        "approvals",
        ["tenant_id", "campaign_id", "status"],
    )


def upgrade() -> None:
    _rebuild(_T06_ACTIONS)


def downgrade() -> None:
    count = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM approvals WHERE approval_action IN "
                "('assortment_submission_approval', 'merchant_notification_approval')"
            )
        )
        .scalar_one()
    )
    if int(count) != 0:
        raise RuntimeError("conditional T06 approvals must be resolved before downgrade")
    _rebuild(_BASE_ACTIONS)
