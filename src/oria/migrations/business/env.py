from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

target_metadata = None


def run_migrations_offline() -> None:
    config = context.config
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table="alembic_version_business",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    config = context.config
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table="alembic_version_business",
        )
        with context.begin_transaction():
            context.run_migrations()


if hasattr(context, "config"):
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
