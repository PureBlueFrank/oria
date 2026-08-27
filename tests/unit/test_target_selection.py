import pytest

from oria._internal.target_selection import (
    TargetSelectionError,
    parse_targets,
    require_explicit_targets,
)

pytestmark = pytest.mark.unit


def test_parse_targets_normalizes_order_and_duplicates() -> None:
    assert parse_targets("redis, postgres,redis", {"postgres", "redis"}, "enterprise") == (
        "postgres",
        "redis",
    )


@pytest.mark.parametrize("raw_targets", [None, "", " , "])
def test_parse_targets_rejects_empty_selection(raw_targets: str | None) -> None:
    with pytest.raises(TargetSelectionError, match="must not be empty"):
        parse_targets(raw_targets, {"deepseek"}, "live")


def test_parse_targets_rejects_unknown_target() -> None:
    with pytest.raises(TargetSelectionError, match=r"unknown live target.*other"):
        parse_targets("deepseek,other", {"deepseek"}, "live")


def test_require_explicit_targets_rejects_missing_switch() -> None:
    with pytest.raises(TargetSelectionError, match="explicit run switch"):
        require_explicit_targets(
            run_flag=None,
            raw_targets="deepseek",
            allowed_targets={"deepseek"},
            kind="live",
        )
