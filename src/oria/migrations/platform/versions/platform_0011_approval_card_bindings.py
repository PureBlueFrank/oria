"""Persist opaque approval card references bound to trusted identities and messages."""

import sqlalchemy as sa
from alembic import op

revision = "platform_0011"
down_revision = "platform_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approval_card_bindings",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("action_hash", sa.String(), nullable=False),
        sa.Column("approval_id", sa.String(), nullable=False),
        sa.Column("approval_revision", sa.String(), nullable=False),
        sa.Column("decision_intent", sa.String(), nullable=False),
        sa.Column("issuer", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("chat_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "action_hash"),
        sa.CheckConstraint(
            "decision_intent = 'approve'",
            name="ck_approval_card_bindings_approve_intent",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "approval_id"], ["approvals.tenant_id", "approvals.approval_id"]
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text('ALTER TABLE "approval_card_bindings" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text('ALTER TABLE "approval_card_bindings" FORCE ROW LEVEL SECURITY'))
        op.execute(
            sa.text(
                'CREATE POLICY oria_tenant_isolation ON "approval_card_bindings" '
                "USING (tenant_id = NULLIF(current_setting('oria.tenant_id', true), '')) "
                "WITH CHECK (tenant_id = NULLIF(current_setting('oria.tenant_id', true), ''))"
            )
        )


def downgrade() -> None:
    op.drop_table("approval_card_bindings")
