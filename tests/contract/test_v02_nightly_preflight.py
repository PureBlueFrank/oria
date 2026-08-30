"""Nightly target and hard-budget preflight contracts."""

from datetime import datetime
from pathlib import Path

import pytest

from oria.eval import (
    NightlyBudget,
    NightlyBudgetExceeded,
    NightlyBudgetLedger,
    NightlyConfigError,
    NightlyProviderResponse,
    NightlyRequest,
    TokenPrices,
    load_nightly_config,
    preflight_nightly_target,
    run_nightly_requests,
)

pytestmark = pytest.mark.contract

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _ROOT / "eval" / "config" / "nightly.yaml"
_PRICING = _ROOT / "eval" / "config" / "pricing"


def test_missing_credential_blocks_before_any_request() -> None:
    result = preflight_nightly_target(
        config_path=_CONFIG,
        pricing_dir=_PRICING,
        target_id="deepseek",
        environ={},
        now=datetime.fromisoformat("2026-08-30T12:00:00+08:00"),
        known_targets=frozenset({"deepseek"}),
    )

    assert result.status == "blocked"
    assert result.request_count == 0
    assert result.reason == "nightly credential is missing"


def test_unknown_target_blocks_before_configured_target_lookup() -> None:
    result = preflight_nightly_target(
        config_path=_CONFIG,
        pricing_dir=_PRICING,
        target_id="unknown",
        environ={"DEEPSEEK_API_KEY": "test-only"},
        now=datetime.fromisoformat("2026-08-30T12:00:00+08:00"),
        known_targets=frozenset({"deepseek"}),
    )

    assert result.status == "blocked"
    assert result.request_count == 0
    assert result.reason == "nightly target is unknown"


def test_expired_pricing_blocks_before_dataset_or_provider_setup() -> None:
    result = preflight_nightly_target(
        config_path=_CONFIG,
        pricing_dir=_PRICING,
        target_id="deepseek",
        environ={"DEEPSEEK_API_KEY": "test-only"},
        now=datetime.fromisoformat("2026-10-01T00:00:00+08:00"),
        known_targets=frozenset({"deepseek"}),
    )

    assert result.status == "blocked"
    assert result.request_count == 0
    assert result.reason == "pricing snapshot is expired"


def test_non_positive_budget_is_invalid_configuration(tmp_path: Path) -> None:
    config = tmp_path / "nightly.yaml"
    config.write_text(
        _CONFIG.read_text(encoding="utf-8").replace("max_cases: 12", "max_cases: 0"),
        encoding="utf-8",
    )

    with pytest.raises(NightlyConfigError, match="unavailable or invalid"):
        load_nightly_config(config)


def test_reviewed_dataset_is_ready_when_key_and_price_are_valid() -> None:
    result = preflight_nightly_target(
        config_path=_CONFIG,
        pricing_dir=_PRICING,
        target_id="deepseek",
        environ={"DEEPSEEK_API_KEY": "test-only"},
        now=datetime.fromisoformat("2026-08-30T12:00:00+08:00"),
        known_targets=frozenset({"deepseek"}),
    )

    assert result.status == "ready"
    assert result.request_count == 0
    assert result.reason is None
    assert result.dataset_version == "1"


def test_pending_dataset_blocks_even_when_key_and_price_are_valid(
    pending_rag_manifest: Path,
) -> None:
    config_dir = pending_rag_manifest.parents[2] / "config"
    config_dir.mkdir()
    config = config_dir / "nightly.yaml"
    config.write_text(_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")

    result = preflight_nightly_target(
        config_path=config,
        pricing_dir=_PRICING,
        target_id="deepseek",
        environ={"DEEPSEEK_API_KEY": "test-only"},
        now=datetime.fromisoformat("2026-08-30T12:00:00+08:00"),
        known_targets=frozenset({"deepseek"}),
    )

    assert result.status == "blocked"
    assert result.request_count == 0
    assert result.reason == "RAG dataset is pending actual human review"


def test_budget_is_reserved_before_request_and_incomplete_run_cannot_complete() -> None:
    budget = NightlyBudget(
        max_cases=1,
        max_input_tokens=100,
        max_output_tokens=20,
        max_cost_usd=1.0,
        max_wall_seconds=30,
    )
    prices = TokenPrices(
        input_cache_hit_per_million_usd=0.1,
        input_cache_miss_per_million_usd=0.2,
        output_per_million_usd=0.3,
        reasoning_per_million_usd=0.3,
    )
    ledger = NightlyBudgetLedger(budget, prices, clock=lambda: 0.0)
    reservation = ledger.reserve(input_tokens=80, max_output_tokens=20)

    assert ledger.complete(expected_cases=1) is False
    ledger.settle(
        reservation,
        cache_hit_tokens=20,
        cache_miss_tokens=50,
        output_tokens=10,
        reasoning_tokens=0,
    )
    assert ledger.complete(expected_cases=1) is True
    with pytest.raises(NightlyBudgetExceeded, match="case budget"):
        ledger.reserve(input_tokens=1, max_output_tokens=1)


def test_reasoning_tokens_are_a_subset_of_output_and_not_double_charged() -> None:
    budget = NightlyBudget(
        max_cases=1,
        max_input_tokens=100,
        max_output_tokens=20,
        max_cost_usd=1.0,
        max_wall_seconds=30,
    )
    prices = TokenPrices(
        input_cache_hit_per_million_usd=1.0,
        input_cache_miss_per_million_usd=1.0,
        output_per_million_usd=2.0,
        reasoning_per_million_usd=2.0,
    )
    ledger = NightlyBudgetLedger(budget, prices, clock=lambda: 0.0)
    reservation = ledger.reserve(input_tokens=100, max_output_tokens=20)

    cost = ledger.settle(
        reservation,
        cache_hit_tokens=0,
        cache_miss_tokens=100,
        output_tokens=20,
        reasoning_tokens=10,
    )

    assert cost == pytest.approx(0.00014)
    assert ledger.totals().output_tokens == 20


@pytest.mark.asyncio
async def test_complete_bounded_nightly_sample_passes() -> None:
    config = load_nightly_config(_CONFIG)
    target = config.targets[0].model_copy(
        update={"budget": config.targets[0].budget.model_copy(update={"max_cases": 2})}
    )
    requests = (
        NightlyRequest(
            case_id="case-1", repetition=1, input_tokens_reserved=100, max_output_tokens=20
        ),
        NightlyRequest(
            case_id="case-1", repetition=2, input_tokens_reserved=100, max_output_tokens=20
        ),
    )

    async def invoke(request: NightlyRequest) -> NightlyProviderResponse:
        return NightlyProviderResponse(
            request_id=f"req-{request.repetition}",
            model=target.model,
            input_tokens=40,
            output_tokens=10,
            reasoning_tokens=2,
            latency_ms=5.0,
        )

    card = await run_nightly_requests(
        target=target,
        prices=TokenPrices(
            input_cache_hit_per_million_usd=0.1,
            input_cache_miss_per_million_usd=0.2,
            output_per_million_usd=0.3,
            reasoning_per_million_usd=0.3,
        ),
        requests=requests,
        dataset_version="1",
        invoke=invoke,
        clock=lambda: 0.0,
    )

    assert card.status == "passed"
    assert card.request_count == 2
    assert card.completed_request_count == 2
    assert [record.request_id for record in card.cases] == ["req-1", "req-2"]


@pytest.mark.asyncio
async def test_provider_failure_cannot_pass_an_incomplete_nightly_sample() -> None:
    config = load_nightly_config(_CONFIG)
    target = config.targets[0].model_copy(
        update={"budget": config.targets[0].budget.model_copy(update={"max_cases": 2})}
    )
    requests = (
        NightlyRequest(
            case_id="case-1", repetition=1, input_tokens_reserved=100, max_output_tokens=20
        ),
        NightlyRequest(
            case_id="case-1", repetition=2, input_tokens_reserved=100, max_output_tokens=20
        ),
    )

    async def invoke(request: NightlyRequest) -> NightlyProviderResponse:
        if request.repetition == 2:
            raise RuntimeError("upstream body must not be copied")
        return NightlyProviderResponse(
            request_id="req-1",
            model=target.model,
            input_tokens=40,
            output_tokens=10,
            latency_ms=5.0,
        )

    card = await run_nightly_requests(
        target=target,
        prices=TokenPrices(
            input_cache_hit_per_million_usd=0.1,
            input_cache_miss_per_million_usd=0.2,
            output_per_million_usd=0.3,
            reasoning_per_million_usd=0.3,
        ),
        requests=requests,
        dataset_version="1",
        invoke=invoke,
        clock=lambda: 0.0,
    )

    assert card.status == "failed"
    assert card.request_count == 2
    assert card.completed_request_count == 1
    assert card.reason == "provider request failed: RuntimeError"
    assert "upstream body" not in card.model_dump_json()
