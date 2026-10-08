"""Store tenant-scoped ingress acceptance and replay identities without message bodies."""

import sqlalchemy as sa
from alembic import op

revision = "platform_0010"
down_revision = "platform_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingress_receipts",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("dedupe_key", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("receipt_ref", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "dedupe_key"),
    )
    op.create_table(
        "ingress_replays",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("replay_key", sa.String(), nullable=False),
        sa.Column("dedupe_key", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "replay_key"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "dedupe_key"],
            ["ingress_receipts.tenant_id", "ingress_receipts.dedupe_key"],
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        for table in ("ingress_receipts", "ingress_replays"):
            op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
            op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
            op.execute(
                sa.text(
                    f'CREATE POLICY oria_tenant_isolation ON "{table}" '
                    "USING (tenant_id = NULLIF(current_setting('oria.tenant_id', true), '')) "
                    "WITH CHECK (tenant_id = "
                    "NULLIF(current_setting('oria.tenant_id', true), ''))"
                )
            )


def downgrade() -> None:
    op.drop_table("ingress_replays")
    op.drop_table("ingress_receipts")
