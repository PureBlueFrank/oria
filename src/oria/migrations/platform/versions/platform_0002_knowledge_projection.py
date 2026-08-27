"""Add versioned ACL/projection metadata and the derived rule snapshot cache."""

import sqlalchemy as sa
from alembic import op

revision = "platform_0002"
down_revision = "platform_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "document_versions",
        sa.Column("acl_json", sa.Text(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "document_versions",
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "document_versions",
        sa.Column("chunking_version", sa.String(), nullable=False, server_default="json-v1"),
    )
    op.add_column(
        "document_versions",
        sa.Column("embedding_profile", sa.String(), nullable=False, server_default="unknown"),
    )
    op.add_column(
        "document_versions", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "rule_snapshot_cache",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("snapshot_id", sa.String(), nullable=False),
        sa.Column("snapshot_hash", sa.String(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "snapshot_id"),
        sa.UniqueConstraint("tenant_id", "snapshot_hash"),
    )


def downgrade() -> None:
    op.drop_table("rule_snapshot_cache")
    op.drop_column("document_versions", "deleted_at")
    op.drop_column("document_versions", "embedding_profile")
    op.drop_column("document_versions", "chunking_version")
    op.drop_column("document_versions", "metadata_json")
    op.drop_column("document_versions", "acl_json")
    op.drop_column("documents", "deleted_at")
