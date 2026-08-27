import os
from collections.abc import Iterable

import pytest

from oria._internal.target_selection import (
    ENTERPRISE_TARGETS,
    LIVE_TARGETS,
    TargetSelectionError,
    require_explicit_targets,
)


def _has_marker(item: pytest.Item, marker: str) -> bool:
    return item.get_closest_marker(marker) is not None


def _deselect(config: pytest.Config, items: list[pytest.Item], markers: Iterable[str]) -> None:
    marker_names = frozenset(markers)
    deselected = [item for item in items if any(_has_marker(item, name) for name in marker_names)]
    if not deselected:
        return
    items[:] = [item for item in items if item not in deselected]
    config.hook.pytest_deselected(items=deselected)


def _validate_external_selection(
    *,
    kind: str,
    run_flag_name: str,
    targets_name: str,
    allowed_targets: frozenset[str],
) -> tuple[str, ...]:
    try:
        return require_explicit_targets(
            run_flag=os.getenv(run_flag_name),
            raw_targets=os.getenv(targets_name),
            allowed_targets=allowed_targets,
            kind=kind,
        )
    except TargetSelectionError as exc:
        pytest.exit(f"blocked: {exc}", returncode=2)


def pytest_sessionstart(session: pytest.Session) -> None:
    mark_expression = session.config.getoption("markexpr").strip()
    if mark_expression == "live":
        _validate_external_selection(
            kind="live",
            run_flag_name="ORIA_RUN_LIVE",
            targets_name="ORIA_LIVE_TARGETS",
            allowed_targets=LIVE_TARGETS,
        )
    elif mark_expression == "enterprise":
        _validate_external_selection(
            kind="enterprise",
            run_flag_name="ORIA_RUN_ENTERPRISE",
            targets_name="ORIA_ENTERPRISE_TARGETS",
            allowed_targets=ENTERPRISE_TARGETS,
        )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    selected_live = [item for item in items if _has_marker(item, "live")]
    if selected_live:
        if os.getenv("ORIA_RUN_LIVE") == "1":
            _validate_external_selection(
                kind="live",
                run_flag_name="ORIA_RUN_LIVE",
                targets_name="ORIA_LIVE_TARGETS",
                allowed_targets=LIVE_TARGETS,
            )
        else:
            _deselect(config, items, ["live"])

    selected_enterprise = [item for item in items if _has_marker(item, "enterprise")]
    if selected_enterprise:
        if os.getenv("ORIA_RUN_ENTERPRISE") == "1":
            _validate_external_selection(
                kind="enterprise",
                run_flag_name="ORIA_RUN_ENTERPRISE",
                targets_name="ORIA_ENTERPRISE_TARGETS",
                allowed_targets=ENTERPRISE_TARGETS,
            )
        else:
            _deselect(config, items, ["enterprise"])

    if os.getenv("ORIA_RUN_PERFORMANCE") != "1":
        _deselect(config, items, ["performance"])
