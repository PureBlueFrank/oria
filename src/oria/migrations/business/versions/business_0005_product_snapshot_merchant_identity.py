"""Bind Business product snapshots to their owning merchant."""

import sqlalchemy as sa
from alembic import op

revision = "business_0005"
down_revision = "business_0004"
branch_labels = None
depends_on = None

_NAMING = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
}


def _constraint_name(table: str, kind: str, columns: tuple[str, ...]) -> str:
    inspector = sa.inspect(op.get_bind())
    if kind == "unique":
        for constraint in inspector.get_unique_constraints(table):
            if tuple(constraint["column_names"]) == columns and constraint.get("name"):
                return str(constraint["name"])
    else:
        for foreign_key in inspector.get_foreign_keys(table):
            if tuple(foreign_key["constrained_columns"]) == columns and foreign_key.get("name"):
                return str(foreign_key["name"])
    raise RuntimeError(f"required {kind} constraint is unavailable on {table}")


def _upgrade_postgres() -> None:
    legacy_unique = _constraint_name(
        "product_snapshots",
        "unique",
        ("tenant_id", "product_ref", "product_version"),
    )
    legacy_product_fk = _constraint_name(
        "enrollment_items",
        "foreignkey",
        ("tenant_id", "product_snapshot_id"),
    )
    op.alter_column("product_snapshots", "merchant_id", existing_type=sa.String(), nullable=False)
    op.drop_constraint(legacy_unique, "product_snapshots", type_="unique")
    op.create_unique_constraint(
        "uq_product_snapshots_merchant_product_version",
        "product_snapshots",
        ["tenant_id", "merchant_id", "product_ref", "product_version"],
    )
    op.create_unique_constraint(
        "uq_product_snapshots_enrollment_binding",
        "product_snapshots",
        [
            "tenant_id",
            "merchant_id",
            "product_ref",
            "product_version",
            "product_snapshot_id",
        ],
    )
    op.drop_constraint(legacy_product_fk, "enrollment_items", type_="foreignkey")
    op.create_foreign_key(
        "fk_enrollment_items_product_binding_product_snapshots",
        "enrollment_items",
        "product_snapshots",
        [
            "tenant_id",
            "merchant_id",
            "product_ref",
            "product_version",
            "product_snapshot_id",
        ],
        [
            "tenant_id",
            "merchant_id",
            "product_ref",
            "product_version",
            "product_snapshot_id",
        ],
    )


def _downgrade_postgres() -> None:
    op.drop_constraint(
        "fk_enrollment_items_product_binding_product_snapshots",
        "enrollment_items",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_enrollment_items_tenant_id_product_snapshots",
        "enrollment_items",
        "product_snapshots",
        ["tenant_id", "product_snapshot_id"],
        ["tenant_id", "product_snapshot_id"],
    )
    op.drop_constraint(
        "uq_product_snapshots_enrollment_binding", "product_snapshots", type_="unique"
    )
    op.drop_constraint(
        "uq_product_snapshots_merchant_product_version",
        "product_snapshots",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_product_snapshots_tenant_id",
        "product_snapshots",
        ["tenant_id", "product_ref", "product_version"],
    )
    op.drop_column("product_snapshots", "merchant_id")


def upgrade() -> None:
    op.add_column("product_snapshots", sa.Column("merchant_id", sa.String(), nullable=True))
    op.execute(
        "UPDATE product_snapshots SET merchant_id = COALESCE(("
        "SELECT enrollment_items.merchant_id FROM enrollment_items "
        "WHERE enrollment_items.tenant_id = product_snapshots.tenant_id "
        "AND enrollment_items.product_snapshot_id = product_snapshots.product_snapshot_id "
        "LIMIT 1), 'legacy:' || product_snapshot_id)"
    )
    if op.get_bind().dialect.name == "postgresql":
        _upgrade_postgres()
        return
    with op.batch_alter_table(
        "product_snapshots", recreate="always", naming_convention=_NAMING
    ) as batch:
        batch.alter_column("merchant_id", existing_type=sa.String(), nullable=False)
        batch.drop_constraint("uq_product_snapshots_tenant_id", type_="unique")
        batch.create_unique_constraint(
            "uq_product_snapshots_merchant_product_version",
            ["tenant_id", "merchant_id", "product_ref", "product_version"],
        )
        batch.create_unique_constraint(
            "uq_product_snapshots_enrollment_binding",
            [
                "tenant_id",
                "merchant_id",
                "product_ref",
                "product_version",
                "product_snapshot_id",
            ],
        )
    with op.batch_alter_table(
        "enrollment_items", recreate="always", naming_convention=_NAMING
    ) as batch:
        batch.drop_constraint(
            "fk_enrollment_items_tenant_id_product_snapshots",
            type_="foreignkey",
        )
        batch.create_foreign_key(
            "fk_enrollment_items_product_binding_product_snapshots",
            "product_snapshots",
            [
                "tenant_id",
                "merchant_id",
                "product_ref",
                "product_version",
                "product_snapshot_id",
            ],
            [
                "tenant_id",
                "merchant_id",
                "product_ref",
                "product_version",
                "product_snapshot_id",
            ],
        )
        batch.create_foreign_key(
            "fk_enrollment_items_enrollment_id_enrollments",
            "enrollments",
            ["tenant_id", "enrollment_id"],
            ["tenant_id", "enrollment_id"],
        )
        batch.create_foreign_key(
            "fk_enrollment_items_campaign_merchant_enrollments",
            "enrollments",
            ["tenant_id", "campaign_id", "merchant_id"],
            ["tenant_id", "campaign_id", "merchant_id"],
        )
        batch.create_unique_constraint(
            "uq_enrollment_items_business_key",
            [
                "tenant_id",
                "campaign_id",
                "merchant_id",
                "product_ref",
                "product_version",
            ],
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _downgrade_postgres()
        return
    with op.batch_alter_table(
        "enrollment_items", recreate="always", naming_convention=_NAMING
    ) as batch:
        batch.drop_constraint(
            "fk_enrollment_items_product_binding_product_snapshots",
            type_="foreignkey",
        )
        batch.create_foreign_key(
            "fk_enrollment_items_tenant_id_product_snapshots",
            "product_snapshots",
            ["tenant_id", "product_snapshot_id"],
            ["tenant_id", "product_snapshot_id"],
        )
        batch.create_unique_constraint(
            "uq_enrollment_items_business_key",
            [
                "tenant_id",
                "campaign_id",
                "merchant_id",
                "product_ref",
                "product_version",
            ],
        )
    with op.batch_alter_table(
        "product_snapshots", recreate="always", naming_convention=_NAMING
    ) as batch:
        batch.drop_constraint("uq_product_snapshots_enrollment_binding", type_="unique")
        batch.drop_constraint("uq_product_snapshots_merchant_product_version", type_="unique")
        batch.create_unique_constraint(
            "uq_product_snapshots_tenant_id",
            ["tenant_id", "product_ref", "product_version"],
        )
        batch.drop_column("merchant_id")
