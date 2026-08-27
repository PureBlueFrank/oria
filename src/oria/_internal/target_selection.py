from collections.abc import Collection


class TargetSelectionError(ValueError):
    """Raised when an explicit external-test target selection is invalid."""


LIVE_TARGETS = frozenset({"anthropic", "deepseek", "kimi", "openai", "zhipu"})
ENTERPRISE_TARGETS = frozenset({"mcp-http", "milvus", "otel", "postgres", "redis"})


def parse_targets(
    raw_targets: str | None, allowed_targets: Collection[str], kind: str
) -> tuple[str, ...]:
    """Parse and validate a non-empty, comma-separated target list."""
    targets = tuple(
        sorted({target.strip() for target in (raw_targets or "").split(",") if target.strip()})
    )
    if not targets:
        raise TargetSelectionError(f"{kind} target list must not be empty")

    unknown_targets = sorted(set(targets).difference(allowed_targets))
    if unknown_targets:
        unknown = ", ".join(unknown_targets)
        raise TargetSelectionError(f"unknown {kind} target(s): {unknown}")
    return targets


def require_explicit_targets(
    *,
    run_flag: str | None,
    raw_targets: str | None,
    allowed_targets: Collection[str],
    kind: str,
) -> tuple[str, ...]:
    """Require the explicit opt-in switch before validating external targets."""
    if run_flag != "1":
        raise TargetSelectionError(f"{kind} tests require the explicit run switch set to 1")
    return parse_targets(raw_targets, allowed_targets, kind)
