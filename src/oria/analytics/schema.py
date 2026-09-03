"""SQLite schema for production-queryable Scenario B analytics facts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

ANALYTICS_SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE analytics_metadata (
    dataset_version TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    generator_version TEXT NOT NULL,
    generator_seed INTEGER NOT NULL CHECK (generator_seed >= 0),
    source TEXT NOT NULL CHECK (source = 'synthetic'),
    contains_real_entities INTEGER NOT NULL CHECK (contains_real_entities = 0),
    license TEXT NOT NULL,
    generated_at TEXT NOT NULL
);

CREATE TABLE funnel_daily (
    tenant_id TEXT NOT NULL,
    event_date TEXT NOT NULL,
    region TEXT NOT NULL,
    category TEXT NOT NULL,
    impressions INTEGER NOT NULL CHECK (impressions >= 0),
    visits INTEGER NOT NULL CHECK (visits BETWEEN 0 AND impressions),
    enrollments INTEGER NOT NULL CHECK (enrollments BETWEEN 0 AND visits),
    confirmations INTEGER NOT NULL CHECK (confirmations BETWEEN 0 AND enrollments),
    redemptions INTEGER NOT NULL CHECK (redemptions BETWEEN 0 AND confirmations),
    PRIMARY KEY (tenant_id, event_date, region, category)
);

CREATE TABLE activity_windows (
    tenant_id TEXT NOT NULL,
    activity_id TEXT NOT NULL,
    region TEXT NOT NULL,
    category TEXT NOT NULL,
    activity_type TEXT NOT NULL,
    starts_on TEXT NOT NULL,
    ends_on TEXT NOT NULL CHECK (starts_on <= ends_on),
    PRIMARY KEY (tenant_id, activity_id)
);

CREATE TABLE market_daily (
    tenant_id TEXT NOT NULL,
    event_date TEXT NOT NULL,
    region TEXT NOT NULL,
    category TEXT NOT NULL,
    market_enrollments INTEGER NOT NULL CHECK (market_enrollments >= 0),
    market_redemptions INTEGER NOT NULL
        CHECK (market_redemptions BETWEEN 0 AND market_enrollments),
    PRIMARY KEY (tenant_id, event_date, region, category)
);

CREATE INDEX ix_funnel_daily_scope
ON funnel_daily (tenant_id, region, category, event_date);

CREATE INDEX ix_activity_windows_scope
ON activity_windows (tenant_id, region, category, ends_on);

CREATE INDEX ix_market_daily_scope
ON market_daily (tenant_id, region, category, event_date);
"""


def create_analytics_schema(database: Path) -> None:
    """Create the query-only analytics schema in a new SQLite database."""
    if database.exists():
        raise FileExistsError("analytics database already exists")
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(_DDL)
        connection.execute(f"PRAGMA user_version = {ANALYTICS_SCHEMA_VERSION}")
