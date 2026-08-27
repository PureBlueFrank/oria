"""Create the minimal knowledge lifecycle catalog."""

import sqlalchemy as sa
from alembic import op

revision = "platform_0001"
down_revision = None
branch_labels = ("platform",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("source_uri", sa.String(), nullable=False),
        sa.Column("owner_ref", sa.String(), nullable=False),
        sa.Column("data_classification", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "document_id"),
    )
    op.create_table(
        "document_versions",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("object_ref", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.document_id"],
        ),
        sa.PrimaryKeyConstraint("tenant_id", "document_id", "version"),
    )
    op.create_table(
        "ingestion_runs",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("document_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id", "document_version"],
            [
                "document_versions.tenant_id",
                "document_versions.document_id",
                "document_versions.version",
            ],
        ),
        sa.PrimaryKeyConstraint("tenant_id", "run_id"),
    )


def downgrade() -> None:
    op.drop_table("ingestion_runs")
    op.drop_table("document_versions")
    op.drop_table("documents")
