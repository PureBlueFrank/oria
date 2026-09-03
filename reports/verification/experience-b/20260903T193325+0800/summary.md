# Workflow 自然语言与终端可视化验证

```yaml
run_id: "20260903T193325+0800"
version: "五项体验优化"
task_id: "任务 B"
depends_on: ["V0.3-Core"]
verification_level: "F | C"
commit: "8f9b765, 28cefa6（测试与本文档提交见 Git 历史）"
executed_at: "2026-09-03T19:33:25+08:00"
environment: "macOS / Python 3.11.15 / uv 0.12.6 / uv.lock f1683586845697ed1095d4266a6656f32fe8f5c41e3637101975bd4716c55075"
provider_model: null
config_fingerprint: "由每次临时目录 Fixture 运行生成，未在报告中持久化"
dataset_version: "demo 1.0.0"
eval_fingerprint: null
commands:
  - "uv run pytest tests/unit/test_cli.py"
  - "uv run pytest tests/unit/test_cli.py tests/unit/test_workflow_presentation.py tests/contract"
  - "uv run pytest tests/unit/test_workflow_presentation.py tests/integration/test_v03_scenario_a_workflow.py -q"
  - "make test"
  - "make lint"
  - "uv run oria workflow start --thread-id sample-human-thread --campaign-id sample-human-campaign --data-dir <临时目录>"
artifacts: []
evidence_refs:
  - "tests/unit/test_workflow_presentation.py"
  - "tests/integration/test_v03_scenario_a_workflow.py"
assertions:
  - "五类 interrupt 均有自然语言、阶段定位和下一步命令"
  - "规则、商家、流程三张终端表存在且只使用公开投影"
  - "未命中商家只展示脱敏 reason code 的数量汇总"
  - "拒绝、失败、unknown/reconciliation、completed 终态可区分"
  - "LocalWorkflowResult.view 不进入 serialization schema，JSON 输出保持原样"
result: "passed"
blocked_by: []
known_limits:
  - "未实现未命中商家逐商家展示；后续需要 PolicyEngine 授权的 MerchantEligibilityDisplayProjection"
  - "本次未运行 Live、Enterprise、Performance、真实网络或真实企业 Adapter"
```

## 实际结果

- 修改前 CLI 基线：`5 passed`。
- 指定 `test_cli.py` + 全部 contract：`201 passed`；加入展示层 unit 后为 `212 passed`。
- 展示语义 + Scenario A 跨 Runtime 集成：`13 passed`。
- 完整非 Live/Enterprise/Performance：`623 passed, 1 deselected`；另有 4 条既有 SQLite migration SAWarning。
- `make lint`：Ruff format `251 files already formatted`，Ruff check 通过，mypy `132 source files` 无问题。
- 人工 CLI smoke：成功停在第 `2/10` 阶段，输出六行规则摘要、10 个合格候选、未命中原因汇总和十步流程表。

## 真实性边界

本验证使用本地 SQLite、官方 AsyncSqliteSaver、MockLLM/Fixture 与合成 demo 数据，只证明 Community 展示、状态投影和兼容契约。没有调用真实模型或企业系统。Graph 拓扑、业务状态机和 JSON serialization schema 均未改变。
