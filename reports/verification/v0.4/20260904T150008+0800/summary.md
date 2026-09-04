# V0.4-T03 动态归因 Agent 验证卡

```yaml
run_id: "20260904T150008+0800"
version: "V0.4"
task_id: "V0.4-T03"
depends_on: ["V0.4-T02", "V0.1-T07"]
verification_level: "CT | IT | SEC | E2E-F | C"
baseline_commit: "f757a82 + working tree"
phase_commits: ["882c5d4", "8b14db7", "Phase 3 由本报告所在提交收口"]
executed_at: "2026-09-04T15:00:08+08:00"
environment: "macOS / Python 3.11.15 / uv 0.12.6 / uv.lock sha256:f1683586845697ed1095d4266a6656f32fe8f5c41e3637101975bd4716c55075"
provider_model: null
dataset_version: "scenario_b_synthetic_v2"
result: "passed"
blocked_by: []
known_limits:
  - "只运行 MockLLM + 本地合成数据 E2E-F，未运行 V0.4-T05 Live。"
  - "未运行 Enterprise、Performance、真实网络或企业 Adapter。"
  - "confidence 仅作未校准解释字段，本卡不声称置信度已校准。"
```

## 交付结果

- 用 `ResearchSpec` 参数化唯一 `research_agent` 循环；场景 A 保留 `rule_result`、`merchant_result`、`proposal` 与原预算语义，同时增加可 checkpoint 的 `tool_results`、`final_result`。
- 新增固定 `attribution_reasoning/v1` 及三态 `AttributionConclusion` schema；冲突结果要求多假设且无唯一结论，证据不足以 schema 内 `abstained=true` 正常结束。
- 引用以 `tool_call_id + tool_name + ToolResult.data JSON Pointer + 原值` 回查；不存在、工具不匹配、路径不存在或数值不匹配均以 `ProposalEvidenceError` 语义不可 repair 失败。
- 归因装配层仅绑定 prompt、5 个只读工具、场景预算和 finalizer；没有复制 model/tools/validate node。
- MockLLM E2E-F 根据首次漏斗返回的实际数值分别选择 `query_activity` 或 `query_market_overview`，证明路径由中间结果驱动。

## 验证结果

```text
修改前基线 make test: 661 passed, 1 deselected, 4 warnings
Phase 1 make lint: Ruff format/check + mypy 139 source files passed
Phase 1 make test: 661 passed, 1 deselected, 4 warnings
Phase 2 定向 schema/prompt: 8 passed
Phase 2 make lint: Ruff format/check + mypy 140 source files passed
Phase 2 make test: 669 passed, 1 deselected, 4 warnings
Phase 3 CT/IT/SEC/UT 定向: 14 passed
最终 make lint: 269 files formatted; Ruff passed; mypy 141 source files passed
最终 make test: 680 passed, 1 deselected, 4 warnings in 333.20s
git diff --check: passed
```

4 条 warning 为既有 SQLite/Alembic 复合外键反射 `SAWarning`，相关 migration 用例通过；本任务未修改 migration、manifest、wheel verifier allowlist 或 `uv.lock`。

## 结论

V0.4-T03 的 Prompt/Agent CT、确定性 E2E-F、非固定路径、三态输出、证据回查、abstain、无进展与预算终止已通过。这些结果只证明 Community/Fixture 机制可执行；不替代 V0.4-T04 冻结评测集或 V0.4-T05 真实模型验证。
