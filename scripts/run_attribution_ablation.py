"""Run the preregistered V0.4-T05 Scenario B remediation ablation.

This diagnostic intentionally allows only the already-exposed sb-v1-043 holdout
case. It never changes the frozen V0.4-T05 card and must not be presented as an
unbiased holdout result.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import yaml

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
from oria.eval.attribution_live import _ANALYSIS_PERIOD, _case_record
from oria.eval.datasets import AttributionGoldenCase, load_golden_dataset
from oria.eval.nightly import PricingSnapshot, TokenPrices

_ALLOWED_EXPOSED_CASE = "sb-v1-043"
_MAX_AUTHORIZED_COST_USD = 8.0


@dataclass(frozen=True, slots=True)
class AblationGroup:
    group_id: str
    label: str
    profile: str
    model: str
    reasoning_effort: Literal["none", "high"]
    max_model_turns: int
    max_tool_calls: int


_GROUPS = (
    AblationGroup(
        "g00_control", "flash / 4 turns / 10 calls", "deepseek", "deepseek-v4-flash", "none", 4, 10
    ),
    AblationGroup(
        "g01_budget", "flash / 6 turns / 20 calls", "deepseek", "deepseek-v4-flash", "none", 6, 20
    ),
    AblationGroup(
        "g10_model",
        "pro thinking / 4 turns / 10 calls",
        "deepseek-pro-thinking",
        "deepseek-v4-pro",
        "high",
        4,
        10,
    ),
    AblationGroup(
        "g11_both",
        "pro thinking / 6 turns / 20 calls",
        "deepseek-pro-thinking",
        "deepseek-v4-pro",
        "high",
        6,
        20,
    ),
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default=_ALLOWED_EXPOSED_CASE)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--order-seed", default="scenario-b-remediation-ablation-v1")
    parser.add_argument(
        "--manifest", type=Path, default=Path("eval/datasets/scenario_b/manifest.json")
    )
    parser.add_argument(
        "--pricing",
        type=Path,
        default=Path("eval/config/pricing/deepseek-20260907.yaml"),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("reports/verification/v0.4/20260907-ablation"),
    )
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--max-total-cost-usd", type=float, default=8.0)
    parser.add_argument("--max-input-tokens", type=int, default=96_000)
    parser.add_argument("--max-output-tokens", type=int, default=24_000)
    parser.add_argument("--per-run-max-cost-usd", type=float, default=0.075)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_case(manifest_path: Path, case_id: str) -> AttributionGoldenCase:
    if case_id != _ALLOWED_EXPOSED_CASE:
        raise ValueError(
            f"ablation runner only permits the already-exposed case {_ALLOWED_EXPOSED_CASE}"
        )
    dataset = load_golden_dataset(manifest_path)
    case = next(
        (
            item
            for item in dataset.cases
            if isinstance(item, AttributionGoldenCase) and item.case_id == case_id
        ),
        None,
    )
    if case is None:
        raise ValueError(f"case {case_id} is unavailable")
    if case.split != "holdout" or case.fixture_variant != "mixed_funnel":
        raise ValueError("exposed diagnostic case identity has drifted")
    return case


def _principal(tenant_id: str, *, kind: Literal["human", "service"]) -> Principal:
    return Principal(
        subject_id="ablation-reviewer" if kind == "human" else "ablation-runner",
        tenant_id=tenant_id,
        kind=kind,
        roles=("operator",) if kind == "human" else ("runtime",),
        authn_method="trusted-exposed-case-ablation",
    )


def _ordered_groups(seed: str, repetition: int) -> tuple[AblationGroup, ...]:
    digest = hashlib.sha256(f"{seed}:{repetition}".encode()).digest()
    rng = random.Random(int.from_bytes(digest[:8]))
    groups = list(_GROUPS)
    rng.shuffle(groups)
    return tuple(groups)


def _worst_case_cost(group: AblationGroup, args: argparse.Namespace, prices: TokenPrices) -> float:
    del group
    return (
        args.max_input_tokens * prices.input_cache_miss_per_million_usd
        + args.max_output_tokens
        * max(prices.output_per_million_usd, prices.reasoning_per_million_usd)
    ) / 1_000_000


def _event_sum(events: list[dict[str, Any]], field: str) -> int:
    return sum(
        value
        for event in events
        if isinstance((value := event.get(field)), int) and not isinstance(value, bool)
    )


def _semantic_status(record: dict[str, Any]) -> tuple[bool, str | None]:
    if not record["automated_pass"]:
        return False, None
    conclusion = record.get("conclusion")
    if not isinstance(conclusion, dict) or conclusion.get("outcome") != "conflicting":
        return False, "semantic_outcome_mismatch"
    assessment = conclusion.get("causal_assessment")
    if not isinstance(assessment, dict):
        return False, "semantic_missing_causal_assessment"
    stages = set(assessment.get("anomalous_conversion_stages", []))
    required = {"impression_to_visit", "confirmation_to_redemption"}
    if not required.issubset(stages):
        missing = required.difference(stages)
        if "impression_to_visit" in missing:
            return False, "semantic_missed_upstream"
        return False, "semantic_missed_downstream"
    if assessment.get("shared_mechanism_observed") is not False:
        return False, "semantic_false_shared_mechanism"
    if conclusion.get("conclusion") is not None:
        return False, "semantic_false_single_cause"
    if len(conclusion.get("hypotheses", [])) < 2:
        return False, "semantic_missing_parallel_hypotheses"
    return True, None


def _failure_type(record: dict[str, Any], semantic_failure: str | None) -> str | None:
    termination = record.get("termination_reason")
    direct = {
        "max_model_turns": "budget_model_turns",
        "max_tool_calls": "budget_tool_calls",
        "structured_output_error": "structured_output_error",
        "schema_validation_failed": "schema_validation_failed",
        "evidence_validation_failed": "evidence_validation_failed",
        "provider_failure": "provider_or_infrastructure",
        "deadline_exceeded": "budget_wall_time",
    }
    if isinstance(termination, str) and termination in direct:
        return direct[termination]
    events = record.get("events", [])
    codes = {
        event.get("error_code")
        for event in events
        if isinstance(event, dict) and isinstance(event.get("error_code"), str)
    }
    if "invalid_arguments" in codes:
        return "tool_invalid_arguments"
    if codes.intersection({"unknown_tool", "tool_forbidden", "permission_denied"}):
        return "tool_unknown_or_forbidden"
    if termination is not None:
        return "provider_or_infrastructure"
    return semantic_failure


def _code_hashes() -> dict[str, str]:
    paths = (
        Path("src/oria/agent/graph.py"),
        Path("src/oria/agent/models.py"),
        Path("src/oria/config/resolve.py"),
        Path("src/oria/providers/openai_compat.py"),
        Path("src/oria/prompts/attribution_reasoning/v2.jinja"),
    )
    return {str(path): _sha256(path) for path in paths}


def _validate_args(args: argparse.Namespace, snapshot: PricingSnapshot) -> None:
    if args.repetitions < 5:
        raise ValueError("the preregistered experiment requires at least five repetitions")
    if not 0 < args.max_total_cost_usd <= _MAX_AUTHORIZED_COST_USD:
        raise ValueError("total cost cap must be positive and cannot exceed the authorized $8")
    if min(args.max_input_tokens, args.max_output_tokens, args.timeout_seconds) <= 0:
        raise ValueError("token and timeout limits must be positive")
    if args.per_run_max_cost_usd <= 0:
        raise ValueError("per-run cost cap must be positive")
    now = datetime.now(UTC)
    if snapshot.valid_until.astimezone(UTC) < now:
        raise ValueError("pricing snapshot is expired")
    missing = {group.model for group in _GROUPS}.difference(snapshot.models)
    if missing:
        raise ValueError(f"pricing snapshot misses models: {', '.join(sorted(missing))}")


def _preflight_payload(
    args: argparse.Namespace,
    case: AttributionGoldenCase,
    snapshot: PricingSnapshot,
) -> dict[str, Any]:
    environ = dict(os.environ)
    environ.setdefault("DEEPSEEK_API_KEY", "dry-run-placeholder")
    environ.update({"ORIA_ENVIRONMENT": "test", "ORIA_EMBEDDING_PROFILE": "fixture"})
    profiles: dict[str, dict[str, Any]] = {}
    for group in _GROUPS:
        resolved = resolve_runtime_config(
            runtime_profile="standard",
            llm_profile=group.profile,
            embedding_profile="fixture",
            data_dir=Path(".artifacts/eval/attribution-ablation-preflight") / group.profile,
            environ=environ,
        )
        if resolved.llm.model != group.model:
            raise ValueError(f"{group.group_id} model identity does not match the plan")
        if resolved.llm.reasoning_effort != group.reasoning_effort:
            raise ValueError(f"{group.group_id} reasoning effort does not match the plan")
        if resolved.llm.structured_output_mode != "native_json_schema":
            raise ValueError(f"{group.group_id} changed the structured-output mode")
        profiles[group.group_id] = {
            "profile": group.profile,
            "model": resolved.llm.model,
            "api_dialect": resolved.llm.api_dialect,
            "structured_output_mode": resolved.llm.structured_output_mode,
            "reasoning_effort": resolved.llm.reasoning_effort,
            "config_fingerprint": resolved.config_fingerprint,
        }
    orders = {
        str(repetition): [group.group_id for group in _ordered_groups(args.order_seed, repetition)]
        for repetition in range(1, args.repetitions + 1)
    }
    worst_total = (
        sum(_worst_case_cost(group, args, snapshot.models[group.model].peak) for group in _GROUPS)
        * args.repetitions
    )
    return {
        "schema_version": 1,
        "experiment": "scenario_b_remediation_ablation_v1",
        "diagnostic_only": True,
        "case_id": case.case_id,
        "case_split": case.split,
        "fixture_variant": case.fixture_variant,
        "repetitions": args.repetitions,
        "expected_runs": args.repetitions * len(_GROUPS),
        "order_seed": args.order_seed,
        "orders": orders,
        "profiles": profiles,
        "limits": {
            "max_total_cost_usd": args.max_total_cost_usd,
            "max_input_tokens": args.max_input_tokens,
            "max_output_tokens": args.max_output_tokens,
            "per_run_max_cost_usd": args.per_run_max_cost_usd,
            "timeout_seconds": args.timeout_seconds,
        },
        "worst_case_reserved_cost_usd": worst_total,
        "pricing_snapshot_id": snapshot.snapshot_id,
        "pricing_sha256": _sha256(args.pricing),
        "manifest_sha256": _sha256(args.manifest),
        "prompt_version": attribution_research_spec().prompt_version,
        "code_hashes": _code_hashes(),
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _aggregate(run_payloads: list[dict[str, Any]], expected_runs: int) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    for group in _GROUPS:
        selected = [item for item in run_payloads if item["group_id"] == group.group_id]
        failures: dict[str, int] = {}
        for item in selected:
            failure = item.get("primary_failure_type")
            if failure is not None:
                failures[failure] = failures.get(failure, 0) + 1
        latencies = [float(item["latency_ms"]) / 1000 for item in selected]
        calls = [float(item["tool_calls_total"]) for item in selected]
        groups[group.group_id] = {
            "label": group.label,
            "completed_runs": len(selected),
            "clean_passes": sum(bool(item["clean_pass"]) for item in selected),
            "clean_pass_rate": (
                sum(bool(item["clean_pass"]) for item in selected) / len(selected)
                if selected
                else None
            ),
            "safe_fail_closed": sum(bool(item["safe_fail_closed"]) for item in selected),
            "failure_types": failures,
            "mean_tool_calls": statistics.fmean(calls) if calls else None,
            "p95_tool_calls": _percentile(calls, 0.95),
            "median_latency_seconds": statistics.median(latencies) if latencies else None,
            "p95_latency_seconds": _percentile(latencies, 0.95),
            "input_tokens": sum(int(item["input_tokens"]) for item in selected),
            "output_tokens": sum(int(item["output_tokens"]) for item in selected),
            "reasoning_tokens": sum(int(item["reasoning_tokens"]) for item in selected),
            "cost_usd": sum(float(item["cost_usd"]) for item in selected),
        }
    return {
        "status": "complete" if len(run_payloads) == expected_runs else "in_progress",
        "completed_runs": len(run_payloads),
        "expected_runs": expected_runs,
        "total_cost_usd": sum(float(item["cost_usd"]) for item in run_payloads),
        "groups": groups,
    }


def _write_summary(run_dir: Path, preflight: dict[str, Any], runs: list[dict[str, Any]]) -> None:
    aggregate = _aggregate(runs, int(preflight["expected_runs"]))
    payload = {"preflight": preflight, "aggregate": aggregate, "runs": runs}
    (run_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# Scenario B remediation ablation",
        "",
        "> Diagnostic use only: sb-v1-043 was previously exposed. This does not replace "
        "the frozen V0.4-T05 failed card.",
        "",
        f"- Status: `{aggregate['status']}` "
        f"({aggregate['completed_runs']}/{aggregate['expected_runs']})",
        f"- Conservative recorded cost: `${aggregate['total_cost_usd']:.6f}` / `$8.00`",
        f"- Pricing snapshot: `{preflight['pricing_snapshot_id']}`",
        "",
        "| Group | Runs | Clean pass | Safe fail-closed | Mean calls | "
        "Median latency (s) | Cost (USD) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for group in _GROUPS:
        stats = aggregate["groups"][group.group_id]
        mean_calls = stats["mean_tool_calls"]
        median_latency = stats["median_latency_seconds"]
        lines.append(
            f"| `{group.group_id}` | {stats['completed_runs']} | {stats['clean_passes']} | "
            f"{stats['safe_fail_closed']} | "
            f"{mean_calls if mean_calls is not None else '-'} | "
            f"{median_latency if median_latency is not None else '-'} | "
            f"{stats['cost_usd']:.6f} |"
        )
    lines.extend(("", "## Failure types", ""))
    for group in _GROUPS:
        failures = aggregate["groups"][group.group_id]["failure_types"]
        lines.append(f"- `{group.group_id}`: `{json.dumps(failures, sort_keys=True)}`")
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_existing_runs(run_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((run_dir / "runs").glob("r??-g*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"invalid saved run: {path}")
        records.append(value)
    return records


async def _run(args: argparse.Namespace) -> int:
    snapshot = PricingSnapshot.model_validate(
        yaml.safe_load(args.pricing.read_text(encoding="utf-8"))
    )
    _validate_args(args, snapshot)
    case = _load_case(args.manifest, args.case)
    preflight = _preflight_payload(args, case, snapshot)
    if preflight["worst_case_reserved_cost_usd"] > args.max_total_cost_usd:
        raise ValueError("declared experiment cannot fit inside the authorized cost cap")
    if args.dry_run:
        print(json.dumps(preflight, ensure_ascii=False, indent=2))
        return 0
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise ValueError("DEEPSEEK_API_KEY is required; no paid request was made")

    run_dir = args.run_dir or (args.output_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "runs").mkdir(exist_ok=True)
    preflight_path = run_dir / "preflight.json"
    if preflight_path.exists():
        existing = json.loads(preflight_path.read_text(encoding="utf-8"))
        if existing != preflight:
            raise ValueError("resume preflight fingerprint does not match this experiment")
    else:
        preflight_path.write_text(
            json.dumps(preflight, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    fixture_root = run_dir / "runtime-data"
    query_database = fixture_root / "fixtures" / case.fixture_variant / "analytics.db"
    label_database = fixture_root / "evaluation-only" / case.fixture_variant / "labels.db"
    if not query_database.exists():
        generate_attribution_fixture(
            query_database, label_database, fixture_variant=case.fixture_variant
        )

    runtime_environ = dict(os.environ)
    runtime_environ.update({"ORIA_ENVIRONMENT": "test", "ORIA_EMBEDDING_PROFILE": "fixture"})
    bases: dict[str, Any] = {}
    try:
        for profile in sorted({group.profile for group in _GROUPS}):
            resolved = resolve_runtime_config(
                runtime_profile="standard",
                llm_profile=profile,
                embedding_profile="fixture",
                data_dir=fixture_root / "profiles" / profile,
                environ=runtime_environ,
            )
            await initialize_data(resolved)
            bases[profile] = await build_runtime(resolved)

        graph = build_attribution_graph()
        existing_runs = _load_existing_runs(run_dir)
        completed = {(item["group_id"], int(item["repetition"])) for item in existing_runs}
        _write_summary(run_dir, preflight, existing_runs)
        for repetition in range(1, args.repetitions + 1):
            for group in _ordered_groups(args.order_seed, repetition):
                key = (group.group_id, repetition)
                if key in completed:
                    continue
                prices = snapshot.models[group.model].peak
                used_cost = sum(float(item["cost_usd"]) for item in existing_runs)
                reservation = _worst_case_cost(group, args, prices)
                if used_cost + reservation > args.max_total_cost_usd:
                    raise ValueError("authorized experiment cost cap would be exceeded")

                base = bases[group.profile]
                if base.llm is None:
                    raise ValueError(f"Live profile {group.profile} is unavailable")
                actor = _principal(case.tenant_id, kind="human")
                executor = _principal(case.tenant_id, kind="service")
                runtime = build_attribution_eval_runtime(
                    base,
                    (case,),
                    query_database,
                    llm=base.llm,
                    trusted_actors=(actor,),
                    trusted_executors=(executor,),
                )
                run_id = f"{case.case_id}-{group.group_id}-r{repetition}"
                started = time.perf_counter()
                try:
                    state = await graph.ainvoke(
                        initial_attribution_state(
                            question=case.question,
                            analysis_period=_ANALYSIS_PERIOD,
                            conversation_history=case.conversation_history,
                        ),
                        context=ResearchRunContext(
                            ctx=runtime.new_context(
                                actor=actor,
                                executor=executor,
                                session_id="scenario-b-remediation-ablation-v1",
                                thread_id=run_id,
                                run_id=run_id,
                            ),
                            limits=ResearchLimits(
                                max_model_turns=group.max_model_turns,
                                max_tool_calls=group.max_tool_calls,
                                max_input_tokens=args.max_input_tokens,
                                max_output_tokens=args.max_output_tokens,
                                max_total_tokens=args.max_input_tokens + args.max_output_tokens,
                                max_cost=args.per_run_max_cost_usd,
                            ),
                            deadline_at=datetime.now(UTC) + timedelta(seconds=args.timeout_seconds),
                        ),
                    )
                finally:
                    await runtime.aclose()
                latency_ms = (time.perf_counter() - started) * 1000
                record_model = _case_record(
                    case=case,
                    repetition=repetition,
                    state=cast(dict[str, Any], state),
                    prices=prices,
                    latency_ms=latency_ms,
                )
                record = record_model.model_dump(mode="json")
                clean_pass, semantic_failure = _semantic_status(record)
                events = cast(list[dict[str, Any]], record["events"])
                reasoning_tokens = _event_sum(events, "reasoning_tokens")
                cache_read_tokens = _event_sum(events, "cache_read_tokens")
                safe_fail_closed = record.get("termination_reason") in {
                    "schema_validation_failed",
                    "evidence_validation_failed",
                }
                payload = {
                    **record,
                    "group_id": group.group_id,
                    "group_label": group.label,
                    "profile": group.profile,
                    "reasoning_effort": group.reasoning_effort,
                    "max_model_turns": group.max_model_turns,
                    "max_tool_calls": group.max_tool_calls,
                    "clean_pass": clean_pass,
                    "safe_fail_closed": safe_fail_closed,
                    "primary_failure_type": _failure_type(record, semantic_failure),
                    "reasoning_tokens": reasoning_tokens,
                    "cache_read_tokens": cache_read_tokens,
                    "completed_at": datetime.now(UTC).isoformat(),
                }
                if record["provider_models"] and set(record["provider_models"]) != {group.model}:
                    raise ValueError(f"provider model identity drifted in {run_id}")
                output_path = run_dir / "runs" / f"r{repetition:02d}-{group.group_id}.json"
                output_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                existing_runs.append(payload)
                completed.add(key)
                _write_summary(run_dir, preflight, existing_runs)
                print(
                    json.dumps(
                        {
                            "group_id": group.group_id,
                            "repetition": repetition,
                            "clean_pass": clean_pass,
                            "failure": payload["primary_failure_type"],
                            "cost_usd": payload["cost_usd"],
                            "cumulative_cost_usd": sum(
                                float(item["cost_usd"]) for item in existing_runs
                            ),
                            "output": str(output_path),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                if group.reasoning_effort == "high" and reasoning_tokens <= 0:
                    raise ValueError(
                        "pro-thinking run did not report reasoning tokens; "
                        "stopped before more paid runs"
                    )
    finally:
        for base in bases.values():
            await base.aclose()
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
