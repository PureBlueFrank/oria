"""Extend existing card bindings with reasoned rejection and read-only details."""

import sqlalchemy as sa
from alembic import op

revision = "platform_0012"
down_revision = "platform_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("approval_card_bindings") as batch:
        batch.drop_constraint("ck_approval_card_bindings_approve_intent", type_="check")
        batch.create_check_constraint(
            "ck_approval_card_bindings_intent", "decision_intent IN ('approve', 'reject', 'view')"
        )
        batch.add_column(
            sa.Column("card_schema", sa.String(), nullable=False, server_default="1.0")
        )
        batch.create_check_constraint(
            "ck_approval_card_bindings_schema", "card_schema IN ('1.0', '2.0')"
        )


def downgrade() -> None:
    # Never discard rejection/detail bindings merely to make an old schema fit.
    count = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM approval_card_bindings WHERE decision_intent <> 'approve'"
            )
        )
        .scalar_one()
    )
    if count:
        raise ValueError("cannot downgrade while rejection or detail bindings exist")
    with op.batch_alter_table("approval_card_bindings") as batch:
        batch.drop_constraint("ck_approval_card_bindings_schema", type_="check")
        batch.drop_column("card_schema")
        batch.drop_constraint("ck_approval_card_bindings_intent", type_="check")
        batch.create_check_constraint(
            "ck_approval_card_bindings_approve_intent", "decision_intent = 'approve'"
        )
