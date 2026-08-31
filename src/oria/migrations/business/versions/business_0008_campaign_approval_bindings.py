"""Persist current campaign versions used to fail closed approval resumes."""

import sqlalchemy as sa
from alembic import op

revision = "business_0008"
down_revision = "business_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaign_approval_bindings",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("enrollment_version", sa.Integer(), nullable=False),
        sa.Column("link_version", sa.Integer(), nullable=False),
        sa.Column("selection_version", sa.String(), nullable=False),
        sa.Column("rule_snapshot_hash", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "campaign_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.campaign_id"],
        ),
        sa.CheckConstraint("enrollment_version >= 1"),
        sa.CheckConstraint("link_version >= 0"),
    )


def downgrade() -> None:
    op.drop_table("campaign_approval_bindings")
