"""Bind approvals to Business versions and track idempotent invalidation facts."""

import sqlalchemy as sa
from alembic import op

revision = "platform_0006"
down_revision = "platform_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.add_column(sa.Column("campaign_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("enrollment_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("link_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("selection_version", sa.String(), nullable=True))
        batch.add_column(sa.Column("selection_hash", sa.String(), nullable=True))
        batch.add_column(sa.Column("rule_snapshot_hash", sa.String(), nullable=True))
        batch.create_index(
            "ix_approvals_tenant_campaign_status",
            ["tenant_id", "campaign_id", "status"],
            unique=False,
        )
    op.create_table(
        "approval_binding_invalidations",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("enrollment_version", sa.Integer(), nullable=False),
        sa.Column("link_version", sa.Integer(), nullable=False),
        sa.Column("selection_version", sa.String(), nullable=False),
        sa.Column("selection_hash", sa.String(), nullable=True),
        sa.Column("rule_snapshot_hash", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "event_id"),
        sa.CheckConstraint("status IN ('pending', 'applied', 'reconciliation')"),
        sa.CheckConstraint("attempt_count >= 0"),
    )


def downgrade() -> None:
    op.drop_table("approval_binding_invalidations")
    with op.batch_alter_table("approvals") as batch:
        batch.drop_index("ix_approvals_tenant_campaign_status")
        batch.drop_column("rule_snapshot_hash")
        batch.drop_column("selection_hash")
        batch.drop_column("selection_version")
        batch.drop_column("link_version")
        batch.drop_column("enrollment_version")
        batch.drop_column("campaign_id")
