"""Generate the frozen demo traces consumed by docs/demo (P1 interactive demo).

Scenario A is driven end-to-end through the real local workflow on Mock LLM,
fixture embedder and Mock enterprise adapters (offline, synthetic data only).
Scenario B distills the already frozen development probe evidence files.
Outputs are plain JS globals so the static page also works over file://.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from oria.config import resolve_runtime_config
from oria.core.types import JsonValue
from oria.orchestrator.local_executor import (
    _STAGES,
    close_enrollment_window,
    complete_selection,
    decide_confirmation,
    decide_local_approval,
    inject_merchant_event,
    inject_selection_decision,
    start_local_workflow,
)

DEMO_DIR = Path("docs/demo")
DATA_DIR = Path(".artifacts/demo-trace-gen")
SCENARIO_B_DIR = Path("reports/verification/v0.4/20260906-remediation/development")
GOLDEN_JSONL = Path("eval/datasets/scenario_b/v1.jsonl")

_SCENARIO_B_CASES = (
    ("sb-v1-001", "20260906T161402128752Z-sb-v1-001.json", "attributed"),
    ("sb-v1-020", "20260906T161657079104Z-sb-v1-020.json", "conflicting"),
    ("sb-v1-015", "20260906T161543486615Z-sb-v1-015.json", "insufficient"),
)
_SCENARIO_B_BLOCKED = "20260906T162341468683Z-sb-v1-020.json"

_THREAD_ID = "scenario-a-demo-trace"
_CAMPAIGN_ID = "campaign-demo-001"


def _interrupt_id(result: Any, kind: str, key: str) -> str:
    for item in result.interrupts:
        if item.get("kind") == kind and isinstance(item.get(key), str):
            return str(item[key])
    raise RuntimeError(f"missing interrupt {kind}/{key}")


def _capture(label: str, action: str, result: Any) -> dict[str, Any]:
    return {
        "label": label,
        "action": action,
        "status": result.status,
        "interrupts": list(result.interrupts),
        "detail": result.detail,
        "view": result.view.model_dump(mode="json"),
    }


async def _scenario_a_trace() -> list[dict[str, Any]]:
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
    DATA_DIR.mkdir(parents=True)
    config = resolve_runtime_config(data_dir=DATA_DIR)
    steps: list[dict[str, Any]] = []

    result = await start_local_workflow(
        config,
        thread_id=_THREAD_ID,
        campaign_id=_CAMPAIGN_ID,
        user_request="生成华东餐饮招商活动并完成预定流程",
    )
    steps.append(_capture("生成规则与活动草案", "workflow start: 零配置自动初始化", result))

    result = await decide_local_approval(
        config,
        thread_id=_THREAD_ID,
        approval_id=_interrupt_id(result, "launch_approval", "approval_id"),
        decision="approve",
        reason=None,
    )
    steps.append(_capture("招商发布审批", "approval approve: LaunchPlan 不可变审批", result))

    result = await inject_merchant_event(
        config,
        thread_id=_THREAD_ID,
        source_event_id="demo-enrollment-event-001",
        merchant_id="demo-m001",
        product_ref="synthetic-product-demo-m001",
    )
    steps.append(_capture("报名与商品圈选", "mock enrollment: 受信事件 inbox 幂等", result))

    result = await close_enrollment_window(
        config,
        thread_id=_THREAD_ID,
        source_event_id="demo-window-close-event-001",
    )
    steps.append(_capture("报名窗口关闭", "mock window-close: 恢复等待中的 Graph", result))

    round_index = 0
    while any(item.get("kind") == "business_confirmation" for item in result.interrupts):
        round_index += 1
        result = await decide_confirmation(
            config,
            thread_id=_THREAD_ID,
            confirmation_task_id=_interrupt_id(
                result, "business_confirmation", "confirmation_task_id"
            ),
            decision="confirm",
        )
        steps.append(
            _capture(
                f"动态业务确认 第 {round_index} 级",
                "workflow resume --decision confirm: 规则动态生成确认链",
                result,
            )
        )

    result = await inject_selection_decision(
        config,
        thread_id=_THREAD_ID,
        source_event_id="demo-selection-decision-event-001",
        selection_version="selection-v1",
        decision="selected",
        reason_code=None,
    )
    steps.append(_capture("提交并等待招后选品", "mock selection-decision: 逐商品决定", result))

    result = await complete_selection(
        config,
        thread_id=_THREAD_ID,
        source_event_id="demo-selection-complete-event-001",
        selection_version="selection-v1",
    )
    steps.append(_capture("选品完成事件", "mock selection-complete: 恢复 Graph", result))

    result = await decide_local_approval(
        config,
        thread_id=_THREAD_ID,
        approval_id=_interrupt_id(result, "consumer_publish_approval", "approval_id"),
        decision="approve",
        reason=None,
    )
    steps.append(
        _capture("C 端投放与商家通知闭环", "approval approve: 终态, 投放 published", result)
    )

    if result.status != "completed":
        raise RuntimeError("scenario A trace did not reach the terminal state")
    return steps


def _golden_questions() -> dict[str, str]:
    questions: dict[str, str] = {}
    for line in GOLDEN_JSONL.read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        questions[case["case_id"]] = case["question"]
    return questions


def _evidence_items(conclusion: dict[str, Any]) -> list[dict[str, JsonValue]]:
    return [
        {
            "tool_name": item["tool_name"],
            "data_path": item["data_path"],
            "value": item["value"],
            "supports": list(item.get("supports", [])),
        }
        for item in conclusion.get("evidence", [])
    ]


def _scenario_b_cases() -> dict[str, Any]:
    questions = _golden_questions()
    cases: list[dict[str, Any]] = []
    for case_id, filename, _ in _SCENARIO_B_CASES:
        payload = json.loads((SCENARIO_B_DIR / filename).read_text(encoding="utf-8"))
        conclusion = payload["conclusion"]
        assessment = conclusion.get("causal_assessment") or {}
        cases.append(
            {
                "case_id": case_id,
                "question": questions[case_id],
                "outcome": conclusion["outcome"],
                "confidence": conclusion["confidence"],
                "confidence_explanation": conclusion["confidence_explanation"],
                "abstained": conclusion["abstained"],
                "conclusion_text": conclusion.get("conclusion"),
                "hypotheses": [
                    {
                        "hypothesis_id": item["hypothesis_id"],
                        "statement": item["statement"],
                        "uncertainty": item["uncertainty"],
                    }
                    for item in conclusion.get("hypotheses", [])
                ],
                "causal_assessment": {
                    "anomalous_conversion_stages": list(
                        assessment.get("anomalous_conversion_stages", [])
                    ),
                    "shared_mechanism_observed": assessment.get("shared_mechanism_observed"),
                    "mechanism_evidence_count": len(assessment.get("mechanism_evidence", [])),
                },
                "requested_data": list(conclusion.get("requested_data", [])),
                "evidence": _evidence_items(conclusion),
                "stats": {
                    "model_turns": payload["model_turns"],
                    "tool_calls_total": payload["tool_calls_total"],
                    "input_tokens": payload["input_tokens"],
                    "output_tokens": payload["output_tokens"],
                    "request_count": len(payload.get("request_ids", [])),
                },
                "evidence_file": f"{SCENARIO_B_DIR.name}/{filename}",
            }
        )

    blocked = json.loads((SCENARIO_B_DIR / _SCENARIO_B_BLOCKED).read_text(encoding="utf-8"))
    blocked_output = blocked.get("structured_output") or {}
    blocked_assessment = blocked_output.get("causal_assessment") or {}
    blocked_sample = {
        "case_id": "sb-v1-020",
        "question": questions["sb-v1-020"],
        "attempted_outcome": blocked_output.get("outcome"),
        "attempted_stages": list(blocked_assessment.get("anomalous_conversion_stages", [])),
        "shared_mechanism_observed": blocked_assessment.get("shared_mechanism_observed"),
        "blocking_rule": (
            "multiple anomalous stages without observed shared mechanism "
            "cannot be attributed to one cause"
        ),
        "termination_reason": (blocked.get("termination") or {}).get("reason"),
        "note": (
            "模型自报两个独立异常环节且未观察到共同机制, 仍提交单因结论; "
            "契约在首次提交与修复轮两次拦停, 未流出任何答案."
        ),
    }
    return {"cases": cases, "blocked_sample": blocked_sample}


def _write_js(name: str, variable: str, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    (DEMO_DIR / "data" / name).write_text(
        f"// Generated by scripts/generate_demo_trace.py; do not edit by hand.\n"
        f"window.{variable} = {body};\n",
        encoding="utf-8",
    )


async def _main() -> None:
    (DEMO_DIR / "data").mkdir(parents=True, exist_ok=True)
    scenario_a_steps = await _scenario_a_trace()
    _write_js(
        "scenario-a.js",
        "ORIA_DEMO_SCENARIO_A",
        {
            "thread_id": _THREAD_ID,
            "campaign_id": _CAMPAIGN_ID,
            "stages": list(_STAGES),
            "steps": scenario_a_steps,
        },
    )
    _write_js("scenario-b.js", "ORIA_DEMO_SCENARIO_B", _scenario_b_cases())
    print(
        json.dumps(
            {
                "scenario_a_steps": len(scenario_a_steps),
                "scenario_b_cases": len(_SCENARIO_B_CASES) + 1,
                "output": str(DEMO_DIR / "data"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(_main())
