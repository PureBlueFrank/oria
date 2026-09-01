"""Persist the exact enrollment-item set sent in each assortment submission."""

import sqlalchemy as sa
from alembic import op

revision = "business_0009"
down_revision = "business_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("assortment_submissions") as batch:
        batch.add_column(sa.Column("selection_version", sa.String(), nullable=True))
        batch.add_column(sa.Column("selection_hash", sa.String(), nullable=True))
        batch.create_check_constraint(
            "ck_assortment_submission_selection_seal",
            "(status = 'completed' AND selection_version IS NOT NULL AND selection_hash IS NOT "
            "NULL) OR (status != 'completed' AND selection_version IS NULL AND selection_hash "
            "IS NULL)",
        )
    with op.batch_alter_table("campaign_approval_bindings") as batch:
        batch.add_column(sa.Column("selection_hash", sa.String(), nullable=True))
    op.create_index(
        "uq_enrollment_items_tenant_campaign_item",
        "enrollment_items",
        ["tenant_id", "campaign_id", "enrollment_item_id"],
        unique=True,
    )
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
            ["tenant_id", "campaign_id", "enrollment_item_id"],
            [
                "enrollment_items.tenant_id",
                "enrollment_items.campaign_id",
                "enrollment_items.enrollment_item_id",
            ],
        ),
    )


def downgrade() -> None:
    op.drop_table("assortment_submission_items")
    op.drop_index(
        "uq_enrollment_items_tenant_campaign_item",
        table_name="enrollment_items",
    )
    with op.batch_alter_table("campaign_approval_bindings") as batch:
        batch.drop_column("selection_hash")
    with op.batch_alter_table("assortment_submissions") as batch:
        batch.drop_constraint("ck_assortment_submission_selection_seal", type_="check")
        batch.drop_column("selection_hash")
        batch.drop_column("selection_version")
