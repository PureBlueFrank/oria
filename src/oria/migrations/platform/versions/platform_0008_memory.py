"""Add tenant and subject scoped long-term memory with vector projections."""

import sqlalchemy as sa
from alembic import op

revision = "platform_0008"
down_revision = "platform_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_items",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("subject_id", sa.String(), nullable=False),
        sa.Column("memory_id", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("provenance", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("sensitivity", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "subject_id", "memory_id"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1"),
    )
    op.create_index(
        "ix_memory_items_namespace_expiry",
        "memory_items",
        ["tenant_id", "subject_id", "deleted_at", "expires_at"],
    )
    op.create_table(
        "memory_embeddings",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("subject_id", sa.String(), nullable=False),
        sa.Column("memory_id", sa.String(), nullable=False),
        sa.Column("projection_id", sa.String(), nullable=False),
        sa.Column("embedding_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "subject_id", "memory_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "subject_id", "memory_id"],
            ["memory_items.tenant_id", "memory_items.subject_id", "memory_items.memory_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_memory_embeddings_namespace_projection",
        "memory_embeddings",
        ["tenant_id", "subject_id", "projection_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_memory_embeddings_namespace_projection",
        table_name="memory_embeddings",
    )
    op.drop_table("memory_embeddings")
    op.drop_index("ix_memory_items_namespace_expiry", table_name="memory_items")
    op.drop_table("memory_items")
