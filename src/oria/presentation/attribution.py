"""Human-first terminal presentation for Scenario B attribution investigations."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from oria.attribution_demo import AttributionAskResult, AttributionToolTrace

_TOOL_ACTIONS = {
    "query_funnel": "查询限定时间、区域与品类的招商漏斗，定位变化发生在哪个环节。",
    "drill_down": "沿已发现的区域或品类继续下钻，检查异常是否集中在局部切片。",
    "query_activity": "核对同一观察窗口内的活动记录与起止边界。",
    "query_market_overview": "查询独立大盘对照，判断变化更像局部异常还是整体波动。",
    "search_history_experience": "检索有权限的历史排查经验，补充待验证路径而不把经验当结论。",
}

_TOOL_REASONS = {
    "query_funnel": "先验证问题前提，并把笼统的转化变化拆到相邻漏斗环节。",
    "drill_down": "已有汇总证据还不能说明影响范围，需要用允许的维度验证局部性。",
    "query_activity": "漏斗变化需要与可观察的业务事件边界交叉校验。",
    "query_market_overview": "活动或局部漏斗的时间重合仍不足以归因，需要独立对照排除大盘共振。",
    "search_history_experience": (
        "当前事实尚未覆盖所有合理路径，历史经验只用于提出下一项可检验假设。"
    ),
}


def _period(value: object) -> str:
    if not isinstance(value, Mapping):
        return "未知时间范围"
    start = value.get("start_date")
    end = value.get("end_date")
    return f"{start}—{end}"


def _evidence_scope(data: Mapping[str, Any]) -> str:
    evidence = data.get("evidence")
    if not isinstance(evidence, Mapping):
        return "工具返回了已校验结果。"
    tables = evidence.get("source_tables")
    sources = (
        "、".join(str(item) for item in tables) if isinstance(tables, Sequence) else "未知来源"
    )
    return (
        f"来源 {sources}，范围 {_period(evidence.get('period'))}，"
        f"返回 {evidence.get('row_count', 0)} 行。"
    )


def _rate(point: object) -> str | None:
    if not isinstance(point, Mapping):
        return None
    metrics = point.get("metrics")
    if not isinstance(metrics, Mapping):
        return None
    value = metrics.get("redemption_rate")
    if not isinstance(value, (int, float)):
        return None
    return f"{value:.2%}"


def _funnel_change_summary(rows: Sequence[object]) -> str:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        label = (
            "/".join(
                str(raw.get(name)) for name in ("region", "category") if raw.get(name) is not None
            )
            or "汇总"
        )
        groups.setdefault(label, []).append(raw)
    summaries: list[str] = []
    for label, points in list(groups.items())[:3]:
        first = _rate(points[0])
        last = _rate(points[-1])
        if first is None or last is None:
            continue
        first_date = points[0].get("event_date")
        last_date = points[-1].get("event_date")
        date_scope = f"{first_date}—{last_date}" if first_date is not None else "当前切片"
        summaries.append(f"{label} {date_scope}: {first} → {last}")
    return f" 各切片核销/确认率：{'；'.join(summaries)}。" if summaries else ""


def _evidence_summary(trace: AttributionToolTrace) -> str:
    if not trace.result.ok or not isinstance(trace.result.data, Mapping):
        code = trace.result.error.code if trace.result.error is not None else "unknown"
        return f"工具未得到可用证据（{code}）。"
    data = trace.result.data
    if trace.tool_name in {"query_funnel", "drill_down"}:
        rows = data.get("rows")
        detail = ""
        if isinstance(rows, Sequence) and rows:
            detail = _funnel_change_summary(rows)
        return _evidence_scope(data) + detail
    if trace.tool_name == "query_activity":
        activities = data.get("activities")
        if not isinstance(activities, Sequence):
            return _evidence_scope(data)
        windows = []
        for item in activities[:3]:
            if isinstance(item, Mapping):
                windows.append(
                    f"{item.get('activity_id')}({item.get('starts_on')}—{item.get('ends_on')})"
                )
        suffix = f"其中 {'、'.join(windows)}。" if windows else "未返回匹配活动。"
        return _evidence_scope(data) + " " + suffix
    if trace.tool_name == "query_market_overview":
        segments = data.get("segments")
        changes: list[str] = []
        if isinstance(segments, Sequence):
            for item in segments[:3]:
                if isinstance(item, Mapping):
                    change = item.get("redemption_rate_change")
                    rendered = f"{change:+.2%}" if isinstance(change, (int, float)) else "无对照"
                    changes.append(f"{item.get('region')}/{item.get('category')} {rendered}")
        suffix = f"核销率变化：{'、'.join(changes)}。" if changes else "没有可用的对照分段。"
        return _evidence_scope(data) + " " + suffix
    if trace.tool_name == "search_history_experience":
        hits = data.get("hits")
        count = len(hits) if isinstance(hits, Sequence) else 0
        return f"返回 {count} 条有引用的历史经验；这些内容仅作待验证线索。"
    return "工具返回了已校验结果。"


def _next_reason(result: AttributionAskResult, index: int) -> str:
    if index + 1 < len(result.tool_trace):
        next_trace = result.tool_trace[index + 1]
        reason = _TOOL_REASONS.get(next_trace.tool_name, "需要另一类证据交叉校验。")
        return f"当前证据仍不足以收敛；下一步选择 {next_trace.tool_name}，因为{reason}"
    if result.conclusion is not None:
        outcome = {
            "attributed": "证据链已收敛为一个最有支持的解释，进入结构化校验。",
            "conflicting": "证据仍支持多个互相冲突的解释，停止强行单一归因。",
            "insufficient": "现有证据不足，按契约弃答并请求补数。",
        }[result.conclusion.outcome]
        return outcome
    assert result.termination is not None
    return f"有界循环触发 {result.termination.reason}，为防止无界调查而终止。"


def _render_conclusion(result: AttributionAskResult) -> list[str]:
    if result.termination is not None:
        usage = result.termination.observed_usage
        return [
            "最终结果：有界调查终止",
            f"原因：{result.termination.reason}",
            (
                f"已用模型轮次 {usage.get('model_turns', 0)}，"
                f"工具调用 {usage.get('tool_calls_total', 0)}。"
            ),
        ]
    assert result.conclusion is not None
    conclusion = result.conclusion
    if conclusion.outcome == "attributed":
        lines = ["最终结果：归因", f"结论：{conclusion.conclusion}"]
    elif conclusion.outcome == "conflicting":
        lines = ["最终结果：证据冲突，不输出唯一归因"]
    else:
        lines = ["最终结果：证据不足，已弃答"]
    for hypothesis in conclusion.hypotheses:
        lines.append(
            f"候选假设 {hypothesis.hypothesis_id}：{hypothesis.statement}"
            f"（不确定性：{hypothesis.uncertainty}）"
        )
    if conclusion.requested_data:
        lines.append("需要补充：" + "；".join(conclusion.requested_data))
    lines.append(f"置信度：{conclusion.confidence:.0%}（{conclusion.confidence_explanation}）")
    return lines


def render_attribution(result: AttributionAskResult) -> str:
    """Render the investigation as an explanatory sequence, with raw detail secondary."""

    if result.mode == "mock_replay":
        mode = "离线 Mock 回放（零网络；只证明演示链路可执行，不证明动态选路质量）"
    else:
        mode = "Live 真实模型（用于验证动态工具选择与归因能力）"
    match_labels = {
        "default_case_id": "默认 case_id 精确选择",
        "case_id_exact": "case_id 精确选择",
        "normalized_question_exact": "Unicode/空白规范化后全句精确匹配",
    }
    lines = [
        "经营异常动态归因 · 场景 B",
        f"模式：{mode}",
        f"案例：{result.case_id} (development)",
        f"user_question：{result.user_question or '未提供（使用案例默认问题）'}",
        f"question_asked：{result.question_asked}",
        f"匹配方式：{match_labels[result.match_method]}",
        f"run_id：{result.run_id}（与 case_id 绑定）",
        "",
        "调查过程",
    ]
    if not result.tool_trace:
        lines.append("未调用业务工具；Agent 直接按证据契约结束。")
    for index, trace in enumerate(result.tool_trace):
        lines.extend(
            [
                "",
                f"第 {trace.sequence} 步 · {trace.tool_name}",
                f"做了什么：{_TOOL_ACTIONS.get(trace.tool_name, '执行一项受限的只读查询。')}",
                f"为什么做：{_TOOL_REASONS.get(trace.tool_name, '需要新证据校验当前假设。')}",
                f"依据什么：{_evidence_summary(trace)}",
                f"下一步为什么继续：{_next_reason(result, index)}",
            ]
        )
    lines.extend(["", *_render_conclusion(result)])
    lines.extend(
        [
            "",
            "工具细节（补充）",
            *(
                f"{item.sequence}. {item.tool_name} [{item.tool_call_id}] 参数 "
                + json.dumps(item.arguments, ensure_ascii=False, sort_keys=True)
                for item in result.tool_trace
            ),
            f"机器可读报告：{result.report_path}",
        ]
    )
    return "\n".join(lines)
