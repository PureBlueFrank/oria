"""Offline runner contract tests; no credentials or network calls."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from oria.agent import initial_research_state
from oria.config import ConfigResolutionError
from oria.eval import scenario_a_live as live
from oria.eval.datasets import load_golden_dataset
from oria.eval.scenario_a import _evaluate_case, _metrics

pytestmark = pytest.mark.unit
MANIFEST = Path(__file__).resolve().parents[2] / "eval/datasets/scenario_a/v1.manifest.json"
ENVIRON = {
    "ORIA_RUN_LIVE": "1",
    "MOONSHOT_API_KEY": "fixture-key",
    "DEEPSEEK_API_KEY": "fixture-key",
}


@pytest.mark.parametrize(
    "target,provider,model",
    [
        ("kimi-k3", "kimi", "kimi-k3"),
        ("deepseek-pro-structured", "deepseek", "deepseek-v4-pro"),
    ],
)
def test_target_switch(tmp_path: Path, target: str, provider: str, model: str) -> None:
    config = live.resolve_scenario_a_live_target(target, data_dir=tmp_path, environ=ENVIRON)
    assert (config.llm.provider, config.llm.model) == (provider, model)
    assert config.runtime_profile == "standard"
    assert config.embedding.provider == "fixture"


@pytest.mark.parametrize("target,environ", [("kimi-k3", {}), ("", ENVIRON), ("mock", ENVIRON)])
def test_requires_live_opt_in_and_real_target(tmp_path: Path, target: str, environ: dict) -> None:
    with pytest.raises(ValueError):
        live.resolve_scenario_a_live_target(target, data_dir=tmp_path, environ=environ)


def test_unknown_target(tmp_path: Path) -> None:
    with pytest.raises(ConfigResolutionError):
        live.resolve_scenario_a_live_target("unknown", data_dir=tmp_path, environ=ENVIRON)


@pytest.mark.asyncio
async def test_runner_reuses_scoring_and_checkpoints_real_context(
    monkeypatch, tmp_path: Path
) -> None:
    knowledge = SimpleNamespace(ingest=AsyncMock())
    ctx = SimpleNamespace(knowledge=knowledge)
    contexts = []

    class Runtime:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        def new_context(self, **kwargs):
            contexts.append(kwargs)
            return ctx

    runtime = Runtime()
    runtime.knowledge = knowledge
    initialize = AsyncMock()
    build = AsyncMock(return_value=runtime)
    monkeypatch.setattr(live, "initialize_data", initialize)
    monkeypatch.setattr(live, "build_runtime", build)
    wrap = Mock(return_value=runtime)
    monkeypatch.setattr(live, "_eval_runtime", wrap)
    state = dict(
        initial_research_state(user_request="offline test", effective_at=live._EFFECTIVE_AT)
    )
    state.update(input_tokens=17, output_tokens=9)
    invoke = AsyncMock(return_value=state)
    monkeypatch.setattr(
        live, "build_research_graph", lambda **kwargs: SimpleNamespace(ainvoke=invoke)
    )
    checkpoints = []
    report = await live.run_scenario_a_live(
        MANIFEST,
        target="kimi-k3",
        data_dir=tmp_path,
        environ=ENVIRON,
        max_new_case_runs=1,
        checkpoint=checkpoints.append,
    )
    expected = await _evaluate_case(load_golden_dataset(MANIFEST).cases[0], state, ctx)
    assert report.status == "in_progress"
    assert report.dataset_case_count == 45
    assert report.cases[0].automated_pass == expected.passed
    assert report.cases[0].failures == expected.failures
    assert report.cases[0].outcome == "runtime_failure"
    assert report.cases[0].tool_sequence == ()
    assert report.cases[0].grounded is None
    assert report.usage.input_tokens == 17
    assert report.usage.output_tokens == 9
    assert report.usage.cost_usd is None
    assert live.ScenarioALiveReport.model_validate_json(report.model_dump_json()) == report
    assert "fixture-key" not in report.model_dump_json()
    assert len(checkpoints) == 2
    assert invoke.call_args.kwargs["context"].ctx is ctx
    assert contexts[-1]["run_id"] == report.cases[0].case_id
    assert wrap.call_args.kwargs == {"mode": "live"}
    assert report.fixture_policy == "real_llm_with_scenario_a_environment_fixtures"
    assert runtime.closed
    initialize.assert_awaited_once()
    build.assert_awaited_once()
    knowledge.ingest.assert_awaited_once()

    complete = await live.run_scenario_a_live(
        MANIFEST,
        target="kimi-k3",
        data_dir=tmp_path,
        environ=ENVIRON,
    )
    assert complete.status == "completed"  # Scoring failures do not enforce Golden gates.
    assert len(complete.cases) == 45
    assert complete.metrics.case_pass_rate < 1

    invoke.side_effect = RuntimeError("secret provider response")
    failed = await live.run_scenario_a_live(
        MANIFEST,
        target="kimi-k3",
        data_dir=tmp_path,
        environ=ENVIRON,
    )
    assert failed.status == "failed"
    assert failed.error_type == "RuntimeError"
    assert "secret provider response" not in failed.model_dump_json()


@pytest.mark.asyncio
async def test_partial_metrics_without_proposals_or_critical_cases() -> None:
    dataset = load_golden_dataset(MANIFEST)
    case = next(case for case in dataset.cases if case.expected_outcome == "runtime_failure")
    case = case.model_copy(update={"critical": False})
    state = {"messages": [], "termination": {"reason": case.expected_reason}}
    result = await _evaluate_case(case, state, SimpleNamespace())
    metrics = _metrics(dataset, (result,))
    assert metrics.critical_pass_rate == 0
    assert metrics.grounded_proposal_rate == 0


@pytest.mark.asyncio
async def test_invalid_batch_fails_before_runtime(monkeypatch, tmp_path: Path) -> None:
    build = AsyncMock()
    monkeypatch.setattr(live, "build_runtime", build)
    with pytest.raises(ValueError, match="positive"):
        await live.run_scenario_a_live(
            MANIFEST,
            target="kimi-k3",
            data_dir=tmp_path,
            environ=ENVIRON,
            max_new_case_runs=0,
        )
    build.assert_not_called()


@pytest.mark.asyncio
async def test_live_defense_accepts_grounded_proposal_instead_of_replay_failure() -> None:
    case = next(
        c for c in load_golden_dataset(MANIFEST).cases if c.fixture_variant == "forged_output"
    )
    state = {
        "messages": [],
        "proposal": {
            "abstained": False,
            "recommended_merchants": [{"merchant_id": "demo-m001"}],
            "unresolved_items": [],
            "field_evidence": {
                "basic.campaign_type": {
                    "type": "citation",
                    "document_id": "rules",
                    "document_version": "1",
                    "chunk_id": "chunk",
                }
            },
        },
    }
    ctx = SimpleNamespace(knowledge=SimpleNamespace(citation_exists=AsyncMock(return_value=True)))
    golden = await _evaluate_case(case, state, ctx)
    assert "outcome_mismatch" in golden.failures
    result = await _evaluate_case(case, state, ctx, mode="live")
    assert result.passed


def _defense_failures(variant: str, **overrides) -> tuple[str, ...]:
    from oria.eval.scenario_a import _case_failures

    case = next(c for c in load_golden_dataset(MANIFEST).cases if c.fixture_variant == variant)
    observed = dict(
        outcome="proposal",
        reason=None,
        tools=(),
        eligible_ids=(),
        recommended_ids=(),
        unresolved_items=(),
        citations_valid=True,
        rule_result=None,
    )
    observed.update(overrides)
    return _case_failures(case, mode="live", **observed)


@pytest.mark.parametrize(
    "variant",
    [
        "forged_output",
        "forged_citation",
        "prompt_injection:write_tool",
        "prompt_injection:bypass_eligibility",
        "invalid_tool_arguments:rule_snapshot_id",
        "duplicate_evidence",
    ],
)
@pytest.mark.parametrize("outcome", ["proposal", "abstain"])
def test_input_attacks_accept_safe_defense(variant: str, outcome: str) -> None:
    assert not _defense_failures(variant, outcome=outcome)


@pytest.mark.parametrize("field", ["eligible_ids", "recommended_ids"])
def test_excluded_merchant_never_passes(field: str) -> None:
    assert "excluded_merchant_present" in _defense_failures(
        "forged_output", **{field: ("demo-m003",)}
    )


def test_forbidden_execution_and_ungrounded_citation_never_pass() -> None:
    assert "forbidden_tool_executed" in _defense_failures(
        "prompt_injection:write_tool", tools=("persist_campaign",)
    )
    assert "citation_not_grounded" in _defense_failures("forged_citation", citations_valid=False)


@pytest.mark.parametrize(
    "case",
    [
        c
        for c in load_golden_dataset(MANIFEST).cases
        if c.fixture_variant.startswith(("missing_rule_category:", "conflicting_rule_category:"))
    ],
    ids=lambda c: c.case_id,
)
def test_rule_fixture_requires_observation_and_abstention(case) -> None:
    assert "required_abstention_missing" in _defense_failures(case.fixture_variant)
    assert "rule_fixture_not_observed" in _defense_failures(
        case.fixture_variant, outcome="abstain", unresolved_items=case.expected_unresolved_items
    )
    observation = {"unresolved_items": list(case.expected_unresolved_items)}
    assert not _defense_failures(
        case.fixture_variant,
        outcome="abstain",
        unresolved_items=case.expected_unresolved_items,
        rule_result=observation,
    )
    assert "unresolved_items_mismatch" in _defense_failures(
        case.fixture_variant, outcome="abstain", rule_result=observation
    )


@pytest.mark.parametrize("tool", ["search_campaign_rules", "query_merchants"])
def test_permission_fixture_requires_stop_and_no_business_execution(tool: str) -> None:
    variant = f"permission_denied:{tool}"
    assert "permission_denial_not_observed" in _defense_failures(variant)
    allowed = ("search_campaign_rules",) if tool == "query_merchants" else ()
    assert not _defense_failures(
        variant, outcome="runtime_failure", reason="policy_or_contract_violation", tools=allowed
    )
    assert "business_tool_executed_after_denial" in _defense_failures(
        variant,
        outcome="runtime_failure",
        reason="policy_or_contract_violation",
        tools=(*allowed, tool),
    )


@pytest.mark.asyncio
async def test_standard_live_keeps_golden_matching() -> None:
    case = next(c for c in load_golden_dataset(MANIFEST).cases if c.fixture_variant == "standard")
    state = {"messages": []}
    golden = await _evaluate_case(case, state, SimpleNamespace())
    observed = await _evaluate_case(case, state, SimpleNamespace(), mode="live")
    assert golden == observed
    assert "outcome_mismatch" in observed.failures


@pytest.mark.asyncio
async def test_live_wrapper_preserves_provider_and_runs_all_environment_fixtures(
    monkeypatch, tmp_path: Path
) -> None:
    from oria.config import resolve_runtime_config
    from oria.core.runtime import build_runtime
    from oria.data import initialize_data
    from oria.eval.scenario_a import _eval_runtime

    dataset = load_golden_dataset(MANIFEST)
    config = resolve_runtime_config(environ={}, data_dir=tmp_path)
    await initialize_data(config)
    async with await build_runtime(config) as base:
        # An offline stand-in for the selected provider. The live wrapper must
        # retain this exact instance, never instantiate its own replay provider.
        provider_runtime = _eval_runtime(base, dataset)
        wrapped = _eval_runtime(provider_runtime, dataset, mode="live")
        assert wrapped.llm is provider_runtime.llm
        await wrapped.aclose()
        monkeypatch.setattr(live, "resolve_scenario_a_live_target", lambda *a, **kw: config)
        monkeypatch.setattr(live, "build_runtime", AsyncMock(return_value=provider_runtime))
        report = await live.run_scenario_a_live(
            MANIFEST, target="offline-test-only", data_dir=tmp_path, environ={}
        )
    assert report.status == "completed"
    assert len(report.cases) == 45
    assert all(c.passed for c in report.cases), [
        (c.case_id, c.failures) for c in report.cases if not c.passed
    ]
    for case, result in zip(dataset.cases, report.cases, strict=True):
        if case.fixture_variant.startswith(
            ("missing_rule_category:", "conflicting_rule_category:")
        ):
            assert result.outcome == "abstain"
            assert result.unresolved_items == case.expected_unresolved_items
        if case.fixture_variant.startswith("permission_denied:"):
            assert result.termination_reason == "policy_or_contract_violation"
            assert result.executed_tools == case.expected_tools
