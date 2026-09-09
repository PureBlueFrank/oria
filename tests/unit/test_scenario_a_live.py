"""Offline runner contract tests; no credentials or network calls."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
