"""Version document ownership/classification and add superseded lifecycle state."""

import sqlalchemy as sa
from alembic import op

revision = "platform_0004"
down_revision = "platform_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_versions",
        sa.Column("owner_ref", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "document_versions",
        sa.Column(
            "data_classification",
            sa.String(),
            nullable=False,
            server_default="internal",
        ),
    )
    op.add_column(
        "document_versions",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE document_versions SET owner_ref = ("
        "SELECT documents.owner_ref FROM documents WHERE "
        "documents.tenant_id = document_versions.tenant_id AND "
        "documents.document_id = document_versions.document_id)"
    )
    op.execute(
        "UPDATE document_versions SET data_classification = ("
        "SELECT documents.data_classification FROM documents WHERE "
        "documents.tenant_id = document_versions.tenant_id AND "
        "documents.document_id = document_versions.document_id)"
    )
    op.execute(
        "UPDATE document_versions AS older SET superseded_at = CURRENT_TIMESTAMP "
        "WHERE older.deleted_at IS NULL AND EXISTS ("
        "SELECT 1 FROM document_versions AS newer WHERE "
        "newer.tenant_id = older.tenant_id AND "
        "newer.document_id = older.document_id AND newer.deleted_at IS NULL AND "
        "(newer.created_at > older.created_at OR "
        "(newer.created_at = older.created_at AND newer.version > older.version)) AND "
        "EXISTS (SELECT 1 FROM ingestion_runs AS completed WHERE "
        "completed.tenant_id = newer.tenant_id AND "
        "completed.document_id = newer.document_id AND "
        "completed.document_version = newer.version AND completed.status = 'completed'))"
    )


def downgrade() -> None:
    op.drop_column("document_versions", "superseded_at")
    op.drop_column("document_versions", "data_classification")
    op.drop_column("document_versions", "owner_ref")
