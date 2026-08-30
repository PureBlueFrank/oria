"""Add the tenant-scoped V0.3 merchant-recruitment workflow schema."""

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "business_0002"
down_revision = "business_0001"
branch_labels = None
depends_on = None


def _identity_columns(entity_id: str) -> tuple[sa.Column[Any], ...]:
    return (
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column(entity_id, sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def _identity_constraints(entity_id: str) -> tuple[sa.PrimaryKeyConstraint, sa.UniqueConstraint]:
    return (
        sa.PrimaryKeyConstraint("tenant_id", entity_id),
        sa.UniqueConstraint("tenant_id", entity_id, "version"),
    )


def upgrade() -> None:
    op.create_table(
        "product_snapshots",
        *_identity_columns("product_snapshot_id"),
        sa.Column("product_ref", sa.String(), nullable=False),
        sa.Column("product_version", sa.String(), nullable=False),
        sa.Column("catalog_snapshot_id", sa.String(), nullable=False),
        sa.Column("attributes_json", sa.Text(), nullable=False),
        *_identity_constraints("product_snapshot_id"),
        sa.UniqueConstraint("tenant_id", "product_ref", "product_version"),
    )
    op.create_table(
        "campaign_rule_snapshot_refs",
        *_identity_columns("campaign_rule_snapshot_ref_id"),
        sa.Column("snapshot_id", sa.String(), nullable=False),
        sa.Column("snapshot_hash", sa.String(), nullable=False),
        *_identity_constraints("campaign_rule_snapshot_ref_id"),
        sa.UniqueConstraint("tenant_id", "snapshot_id", "snapshot_hash"),
    )
    op.create_table(
        "campaigns",
        *_identity_columns("campaign_id"),
        sa.Column("rule_snapshot_ref_id", sa.String(), nullable=False),
        sa.Column("enrollment_mode", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        *_identity_constraints("campaign_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "rule_snapshot_ref_id"],
            [
                "campaign_rule_snapshot_refs.tenant_id",
                "campaign_rule_snapshot_refs.campaign_rule_snapshot_ref_id",
            ],
        ),
        sa.CheckConstraint("enrollment_mode IN ('merchant', 'auto', 'hybrid')"),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_launch_approval', 'recruiting', 'selecting', "
            "'pending_consumer_publish', 'active', 'completed', 'cancelled')"
        ),
    )
    op.create_table(
        "coupon_batches",
        *_identity_columns("coupon_batch_id"),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("coupon_spec_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        *_identity_constraints("coupon_batch_id"),
        sa.UniqueConstraint("tenant_id", "campaign_id", "coupon_spec_hash"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.campaign_id"],
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'materializing', 'ready', 'failed', 'unknown', 'expired')"
        ),
    )
    op.create_table(
        "launch_saga_states",
        *_identity_columns("launch_saga_id"),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("checkpoint", sa.String(), nullable=False),
        *_identity_constraints("launch_saga_id"),
        sa.UniqueConstraint("tenant_id", "campaign_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.campaign_id"],
        ),
        sa.CheckConstraint("status IN ('pending', 'running', 'completed', 'failed', 'unknown')"),
    )
    op.create_table(
        "recruitment_publications",
        *_identity_columns("recruitment_publication_id"),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("merchant_scope_hash", sa.String(), nullable=False),
        sa.Column("material_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("receipt_id", sa.String(), nullable=True),
        *_identity_constraints("recruitment_publication_id"),
        sa.UniqueConstraint("tenant_id", "campaign_id", "merchant_scope_hash", "material_version"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.campaign_id"],
        ),
        sa.CheckConstraint("status IN ('pending', 'published', 'failed', 'unknown')"),
    )
    op.create_table(
        "enrollments",
        *_identity_columns("enrollment_id"),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("merchant_id", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        *_identity_constraints("enrollment_id"),
        sa.UniqueConstraint("tenant_id", "campaign_id", "merchant_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.campaign_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "merchant_id"],
            ["merchants.tenant_id", "merchants.merchant_id"],
        ),
        sa.CheckConstraint("mode IN ('merchant', 'auto', 'hybrid')"),
        sa.CheckConstraint("status IN ('open', 'submitted', 'closed', 'rejected')"),
    )
    op.create_table(
        "enrollment_items",
        *_identity_columns("enrollment_item_id"),
        sa.Column("enrollment_id", sa.String(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("merchant_id", sa.String(), nullable=False),
        sa.Column("product_ref", sa.String(), nullable=False),
        sa.Column("product_version", sa.String(), nullable=False),
        sa.Column("product_snapshot_id", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("sources_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        *_identity_constraints("enrollment_item_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "campaign_id",
            "merchant_id",
            "product_ref",
            "product_version",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id", "merchant_id"],
            ["enrollments.tenant_id", "enrollments.campaign_id", "enrollments.merchant_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "enrollment_id"],
            ["enrollments.tenant_id", "enrollments.enrollment_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "product_snapshot_id"],
            ["product_snapshots.tenant_id", "product_snapshots.product_snapshot_id"],
        ),
        sa.CheckConstraint("mode IN ('merchant', 'auto', 'hybrid')"),
        sa.CheckConstraint(
            "(mode = 'merchant' AND sources_json = '[\"merchant\"]') OR "
            "(mode = 'auto' AND sources_json = '[\"auto\"]') OR "
            "(mode = 'hybrid' AND sources_json IN "
            '(\'["auto"]\', \'["merchant"]\', \'["auto","merchant"]\'))'
        ),
        sa.CheckConstraint("status IN ('pending_confirmation', 'confirmed', 'rejected')"),
    )
    op.create_table(
        "enrollment_coupon_links",
        *_identity_columns("enrollment_coupon_link_id"),
        sa.Column("enrollment_item_id", sa.String(), nullable=False),
        sa.Column("coupon_batch_id", sa.String(), nullable=False),
        sa.Column("benefit_tier", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        *_identity_constraints("enrollment_coupon_link_id"),
        sa.UniqueConstraint("tenant_id", "enrollment_item_id", "coupon_batch_id", "benefit_tier"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "enrollment_item_id"],
            ["enrollment_items.tenant_id", "enrollment_items.enrollment_item_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "coupon_batch_id"],
            ["coupon_batches.tenant_id", "coupon_batches.coupon_batch_id"],
        ),
        sa.CheckConstraint("benefit_tier IN ('base', 'boosted')"),
        sa.CheckConstraint("status IN ('pending', 'active', 'invalid')"),
    )
    op.create_table(
        "confirmation_tasks",
        *_identity_columns("confirmation_task_id"),
        sa.Column("enrollment_item_id", sa.String(), nullable=False),
        sa.Column("subject_type", sa.String(), nullable=False),
        sa.Column("subject_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timeout_action", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        *_identity_constraints("confirmation_task_id"),
        sa.UniqueConstraint("tenant_id", "enrollment_item_id", "sequence"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "enrollment_item_id"],
            ["enrollment_items.tenant_id", "enrollment_items.enrollment_item_id"],
        ),
        sa.CheckConstraint("subject_type IN ('merchant', 'sales', 'sales_manager')"),
        sa.CheckConstraint("timeout_action IN ('reject', 'escalate', 'explicit_auto_confirm')"),
        sa.CheckConstraint("status IN ('pending', 'confirmed', 'rejected', 'timed_out')"),
    )
    op.create_table(
        "assortment_submissions",
        *_identity_columns("assortment_submission_id"),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("submission_version", sa.String(), nullable=False),
        sa.Column("assortment_policy_ref", sa.String(), nullable=False),
        sa.Column("assortment_policy_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        *_identity_constraints("assortment_submission_id"),
        sa.UniqueConstraint("tenant_id", "campaign_id", "submission_version"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.campaign_id"],
        ),
        sa.CheckConstraint("status IN ('pending', 'submitted', 'completed', 'failed', 'unknown')"),
    )
    op.create_table(
        "selection_decisions",
        *_identity_columns("selection_decision_id"),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("submission_version", sa.String(), nullable=False),
        sa.Column("selection_version", sa.String(), nullable=False),
        sa.Column("enrollment_item_id", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("reason_code", sa.String(), nullable=True),
        *_identity_constraints("selection_decision_id"),
        sa.UniqueConstraint("tenant_id", "campaign_id", "selection_version", "enrollment_item_id"),
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
        sa.CheckConstraint("decision IN ('selected', 'rejected')"),
        sa.CheckConstraint("decision = 'selected' OR reason_code IS NOT NULL"),
    )
    op.create_table(
        "consumer_placements",
        *_identity_columns("consumer_placement_id"),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("selection_version", sa.String(), nullable=False),
        sa.Column("placement_spec_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("receipt_id", sa.String(), nullable=True),
        *_identity_constraints("consumer_placement_id"),
        sa.UniqueConstraint("tenant_id", "campaign_id", "selection_version", "placement_spec_hash"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.campaign_id"],
        ),
        sa.CheckConstraint("status IN ('pending', 'published', 'failed', 'unknown')"),
    )
    op.create_table(
        "merchant_notifications",
        *_identity_columns("merchant_notification_id"),
        sa.Column("merchant_id", sa.String(), nullable=False),
        sa.Column("campaign_id", sa.String(), nullable=False),
        sa.Column("result_version", sa.String(), nullable=False),
        sa.Column("template_id", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("receipt_id", sa.String(), nullable=True),
        *_identity_constraints("merchant_notification_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "merchant_id",
            "campaign_id",
            "result_version",
            "template_id",
            "channel",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "merchant_id"],
            ["merchants.tenant_id", "merchants.merchant_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.campaign_id"],
        ),
        sa.CheckConstraint("status IN ('pending', 'sent', 'retrying', 'dead_letter')"),
        sa.CheckConstraint("attempt_count >= 0"),
    )


def downgrade() -> None:
    op.drop_table("merchant_notifications")
    op.drop_table("consumer_placements")
    op.drop_table("selection_decisions")
    op.drop_table("assortment_submissions")
    op.drop_table("confirmation_tasks")
    op.drop_table("enrollment_coupon_links")
    op.drop_table("enrollment_items")
    op.drop_table("enrollments")
    op.drop_table("recruitment_publications")
    op.drop_table("launch_saga_states")
    op.drop_table("coupon_batches")
    op.drop_table("campaigns")
    op.drop_table("campaign_rule_snapshot_refs")
    op.drop_table("product_snapshots")
