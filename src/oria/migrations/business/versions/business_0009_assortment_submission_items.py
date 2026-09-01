"""Persist the exact enrollment-item set sent in each assortment submission."""

import sqlalchemy as sa
from alembic import op

revision = "business_0009"
down_revision = "business_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assortment_submission_items",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("submission_version", sa.String(), nullable=False),
        sa.Column("enrollment_item_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "campaign_id",
            "submission_version",
            "enrollment_item_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id", "submission_version"],
            [
                "assortment_submissions.tenant_id",
                "assortment_submissions.campaign_id",
                "assortment_submissions.submission_version",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "enrollment_item_id"],
            ["enrollment_items.tenant_id", "enrollment_items.enrollment_item_id"],
        ),
    )


def downgrade() -> None:
    op.drop_table("assortment_submission_items")
