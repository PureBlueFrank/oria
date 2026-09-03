"""V0.4-T01 root-cause label physical-isolation tests."""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from oria.eval.attribution_data import generate_attribution_fixture

pytestmark = pytest.mark.security


def _tables(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def test_query_database_has_no_label_table_or_label_payload(tmp_path: Path) -> None:
    query_database = tmp_path / "analytics.db"
    label_database = tmp_path / "evaluation-only" / "labels.db"
    generate_attribution_fixture(query_database, label_database)

    assert _tables(query_database) == {
        "analytics_metadata",
        "funnel_daily",
        "activity_windows",
        "market_daily",
    }
    assert _tables(label_database) == {"attribution_labels"}
    query_bytes = query_database.read_bytes()
    assert b"full_service_campaign_ended" not in query_bytes
    assert "区域下钻异常与活动结束时间相邻".encode() not in query_bytes
    with (
        sqlite3.connect(query_database) as connection,
        pytest.raises(sqlite3.OperationalError, match="no such table"),
    ):
        connection.execute("SELECT * FROM attribution_labels").fetchall()


def test_generator_rejects_a_shared_query_and_label_database(tmp_path: Path) -> None:
    database = tmp_path / "shared.db"

    with pytest.raises(ValueError, match="different databases"):
        generate_attribution_fixture(database, database)

    assert not database.exists()


def test_production_analytics_package_does_not_import_eval_labels() -> None:
    package = Path(__file__).resolve().parents[2] / "src" / "oria" / "analytics"
    imports: set[str] = set()
    for source_path in package.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imports.add(node.module)

    assert all(not name.startswith("oria.eval") for name in imports)
