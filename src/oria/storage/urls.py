"""Secret-preserving database URL conversions for installed adapters."""

from __future__ import annotations

from pydantic import SecretStr


def sqlalchemy_postgres_url(value: SecretStr) -> str:
    """Return the psycopg SQLAlchemy dialect URL without logging the secret."""

    raw = value.get_secret_value()
    if raw.startswith("postgresql+psycopg://"):
        return raw
    if raw.startswith("postgresql://"):
        return "postgresql+psycopg://" + raw.removeprefix("postgresql://")
    if raw.startswith("postgres://"):
        return "postgresql+psycopg://" + raw.removeprefix("postgres://")
    raise ValueError("PostgreSQL URL is incompatible with the psycopg driver")


def psycopg_postgres_url(value: SecretStr) -> str:
    """Return a libpq-compatible URL for psycopg and official checkpoint adapters."""

    raw = value.get_secret_value()
    if raw.startswith("postgresql+psycopg://"):
        return "postgresql://" + raw.removeprefix("postgresql+psycopg://")
    return raw
