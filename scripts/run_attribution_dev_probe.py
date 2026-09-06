"""Run selected Scenario B development cases against one explicit Live LLM profile.

Development-only remediation probe for V0.4-T05. It never touches the frozen
holdout split, the frozen Live target, or golden labels; outputs are redacted
synthetic-data evidence files for human review, not automated acceptance.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from langgraph.checkpoint.memory import InMemorySaver

from oria.agent import (
    ResearchLimits,
    ResearchRunContext,
    attribution_research_spec,
    build_attribution_graph,
    initial_attribution_state,
)
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import Principal
from oria.data import initialize_data
from oria.eval.attribution import build_attribution_eval_runtime
from oria.eval.attribution_data import generate_attribution_fixture
from oria.eval.attribution_live import _ANALYSIS_PERIOD
from oria.eval.datasets import AttributionGoldenCase, load_golden_dataset
from oria.eval.nightly import PricingSnapshot

_WATCHED_CASES = ("sb-v1-001", "sb-v1-015", "sb-v1-020")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="deepseek-pro-structured")
    parser.add_argument(
        "--case",
        dest="cases",
        action="append",
        default=None,
        help="Development case ID; repeatable. Defaults to the three watched cases.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("eval/datasets/scenario_b/manifest.json"),
    )
    parser.add_argument(
        "--pricing",
        type=Path,
        default=Path("eval/config/pricing/deepseek-20260830.yaml"),
    )
    parser.add_argument(
        "--data-dir", type=Path, default=Path(".artifacts/eval/attribution-dev-probe")
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/verification/v0.4/20260906-remediation/development"),
    )
    parser.add_argument("--max-model-turns", type=int, default=12)
    parser.add_argument("--max-tool-calls", type=int, default=20)
    parser.add_argument("--max-input-tokens", type=int, default=200_000)
    parser.add_argument("--max-output-tokens", type=int, default=32_000)
    parser.add_argument("--max-cost", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve config and list selected cases without any paid request.",
    )
    return parser.parse_args()


def _principal(tenant_id: str, *, kind: Literal["human", "service"]) -> Principal:
    return Principal(
        subject_id="dev-probe-reviewer" if kind == "human" else "dev-probe-runner",
        tenant_id=tenant_id,
        kind=kind,
        roles=("operator",) if kind == "human" else ("runtime",),
        authn_method="trusted-dev-probe",
    )


def _select_cases(manifest: Path, case_ids: tuple[str, ...]) -> tuple[AttributionGoldenCase, ...]:
    dataset = load_golden_dataset(manifest)
    selected: list[AttributionGoldenCase] = []
    missing: list[str] = []
    for case_id in case_ids:
        case = next(
            (
                item
                for item in dataset.cases
                if isinstance(item, AttributionGoldenCase) and item.case_id == case_id
            ),
            None,
        )
        if case is None:
            missing.append(case_id)
            continue
        if case.split != "development":
            raise ValueError(f"dev probe only accepts development cases; {case_id} is {case.split}")
        selected.append(case)
    if missing:
        raise ValueError(f"unknown development case IDs: {', '.join(missing)}")
    return tuple(selected)


def _cost_upper_bound(
    snapshot: PricingSnapshot, model: str, input_tokens: int, output_tokens: int
) -> float | None:
    prices = snapshot.models.get(model)
    if prices is None:
        return None
    peak = prices.peak
    return (
        input_tokens * peak.input_cache_miss_per_million_usd
        + output_tokens * peak.output_per_million_usd
    ) / 1_000_000


def _case_payload(
    *,
    case: AttributionGoldenCase,
    state: dict[str, Any],
    profile: str,
    prompt_version: int,
    limits: ResearchLimits,
    cost_upper_bound: float | None,
    pricing_model_covered: bool,
) -> dict[str, Any]:
    events = state.get("events", [])
    request_ids = [
        event.get("provider_request_id")
        for event in events
        if event.get("type") == "model_completed" and event.get("provider_request_id")
    ]
    payload: dict[str, Any] = {
        "termination": state.get("termination"),
        "conclusion": state.get("conclusion"),
        "structured_output": state.get("structured_output"),
        "tool_results": state.get("tool_results", {}),
        "model_turns": state.get("model_turns", 0),
        "tool_calls_total": state.get("tool_calls_total", 0),
        "input_tokens": state.get("input_tokens", 0),
        "output_tokens": state.get("output_tokens", 0),
        "events": events,
        "request_ids": request_ids,
        "case_id": case.case_id,
        "split": case.split,
        "expected_outcome": case.expected_outcome,
        "profile": profile,
        "prompt_version": prompt_version,
        "limits": limits.model_dump(mode="json"),
        "completed_at": datetime.now(UTC).isoformat(),
        "cost_upper_bound_usd": cost_upper_bound,
    }
    if not pricing_model_covered:
        payload["cost_note"] = (
            "pricing snapshot does not cover this model; cost not estimated, "
            "token usage is provider-reported"
        )
    return payload


async def _run(args: argparse.Namespace) -> int:
    case_ids = tuple(args.cases) if args.cases else _WATCHED_CASES
    cases = _select_cases(args.manifest, case_ids)
    snapshot = PricingSnapshot.model_validate(
        yaml.safe_load(args.pricing.read_text(encoding="utf-8"))
    )
    limits = ResearchLimits(
        max_model_turns=args.max_model_turns,
        max_tool_calls=args.max_tool_calls,
        max_input_tokens=args.max_input_tokens,
        max_output_tokens=args.max_output_tokens,
        max_total_tokens=args.max_input_tokens + args.max_output_tokens,
        max_cost=args.max_cost,
    )
    if args.dry_run:
        print(
            json.dumps(
                {
                    "profile": args.profile,
                    "cases": [case.case_id for case in cases],
                    "limits": limits.model_dump(mode="json"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    started_at = datetime.now(UTC)
    run_dir = args.data_dir / "runs" / started_at.strftime("%Y%m%dT%H%M%S%fZ")
    query_databases: dict[str, Path] = {}
    for variant in sorted({case.fixture_variant for case in cases}):
        query_database = run_dir / "fixtures" / variant / "analytics.db"
        label_database = run_dir / "evaluation-only" / variant / "labels.db"
        generate_attribution_fixture(query_database, label_database, fixture_variant=variant)
        query_databases[variant] = query_database

    runtime_environ = dict(os.environ)
    runtime_environ.update({"ORIA_ENVIRONMENT": "test", "ORIA_EMBEDDING_PROFILE": "fixture"})
    resolved = resolve_runtime_config(
        runtime_profile="standard",
        llm_profile=args.profile,
        embedding_profile="fixture",
        data_dir=run_dir / "runtime",
        environ=runtime_environ,
    )
    await initialize_data(resolved)
    base = await build_runtime(resolved)
    if base.llm is None:
        raise RuntimeError("selected Live profile is unavailable")
    spec = attribution_research_spec()
    graph = build_attribution_graph(checkpointer=InMemorySaver())
    summaries: list[dict[str, Any]] = []
    try:
        for case in cases:
            actor = _principal(case.tenant_id, kind="human")
            executor = _principal(case.tenant_id, kind="service")
            runtime = build_attribution_eval_runtime(
                base,
                (case,),
                query_databases[case.fixture_variant],
                llm=base.llm,
                trusted_actors=(actor,),
                trusted_executors=(executor,),
            )
            run_id = f"{case.case_id}-dev-probe"
            ctx = runtime.new_context(
                actor=actor,
                executor=executor,
                session_id="attribution-dev-probe",
                thread_id=run_id,
                run_id=run_id,
            )
            try:
                state = await graph.ainvoke(
                    initial_attribution_state(
                        question=case.question,
                        analysis_period=_ANALYSIS_PERIOD,
                        conversation_history=case.conversation_history,
                    ),
                    config={"configurable": {"thread_id": run_id}},
                    context=ResearchRunContext(
                        ctx=ctx,
                        limits=limits,
                        deadline_at=datetime.now(UTC) + timedelta(seconds=args.timeout_seconds),
                    ),
                )
            finally:
                await runtime.aclose()
            state_dict = dict(state)
            model = resolved.llm.model
            payload = _case_payload(
                case=case,
                state=state_dict,
                profile=args.profile,
                prompt_version=spec.prompt_version,
                limits=limits,
                cost_upper_bound=_cost_upper_bound(
                    snapshot,
                    model,
                    int(state_dict.get("input_tokens", 0)),
                    int(state_dict.get("output_tokens", 0)),
                ),
                pricing_model_covered=model in snapshot.models,
            )
            args.output_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            output_path = args.output_dir / f"{stamp}-{case.case_id}.json"
            output_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            conclusion = payload["conclusion"] or {}
            termination = payload["termination"] or {}
            summaries.append(
                {
                    "case_id": case.case_id,
                    "expected_outcome": case.expected_outcome,
                    "outcome": conclusion.get("outcome"),
                    "termination": termination.get("reason"),
                    "model_turns": payload["model_turns"],
                    "tool_calls_total": payload["tool_calls_total"],
                    "input_tokens": payload["input_tokens"],
                    "output_tokens": payload["output_tokens"],
                    "cost_upper_bound_usd": payload["cost_upper_bound_usd"],
                    "request_ids": payload["request_ids"],
                    "output": str(output_path),
                }
            )
    finally:
        await base.aclose()
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    args = _arguments()
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
