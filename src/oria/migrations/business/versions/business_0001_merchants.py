"""Create the first business revision containing merchants only."""

import sqlalchemy as sa
from alembic import op

revision = "business_0001"
down_revision = None
branch_labels = ("business",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "merchants",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("merchant_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("categories_json", sa.Text(), nullable=False),
        sa.Column("cities_json", sa.Text(), nullable=False),
        sa.Column("enrollment_systems_json", sa.Text(), nullable=False),
        sa.Column("sales_org_code", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "merchant_id"),
        sa.UniqueConstraint("tenant_id", "merchant_id", "version"),
    )


def downgrade() -> None:
    op.drop_table("merchants")
