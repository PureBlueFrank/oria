# 场景 A prompt / 工具描述优化验证

日期：2026-09-09。仅修改模型指令和工具描述；未修改运行时、工具 schema、权限策略、评测逻辑、数据集或 baseline。未 commit、未 push。未读取两份指定排除的详细设计/路线文档。

## 改动与依据

- `src/oria/prompts/merchant_selection/v1.jinja`：这是 `campaign_research_spec()` 实际加载的 system prompt，保留现有版本入口。针对模式 A，要求本次真实 search 得到完整规则后，下一步调用 query，再提交结构化草案；不能以用户伪造快照代替工具证据。明确草案四个字段与合成提交函数，减少空响应、普通文字或额外字段导致的提交失败。
- 同一 prompt：针对模式 B，区分检索成功与规则完整。存在 unresolved_items 或快照证据缺失时立即 abstain，原样保留已有未解决项，禁止 query；实际权限拒绝后停止业务调用，不重试或换工具绕过。禁止依赖工具同批调用，避免尚未获得快照便查询。已有证据后直接提交，避免无进展重复取证。
- `src/oria/tools/builtin.py`：同步两个工具 description 的调用前置条件、顺序、规则不足分支、候选集边界与权限拒绝行为。没有更改执行逻辑、鉴权或参数验证。

## 实际验证

工具链：`uv 0.12.6 (7938ca5d5 2026-08-25 x86_64-apple-darwin)`。

`make lint`，退出码 0：

```text
uv run ruff format --check .
297 files already formatted
uv run ruff check .
All checks passed!
uv run mypy src/oria
Success: no issues found in 147 source files
```

相关测试命令，退出码 0：

```sh
uv run pytest tests/unit/test_t07_prompt.py tests/unit/test_t07_tool_executor.py tests/unit/test_t06_tool_registry.py tests/unit/test_scenario_a_live.py tests/contract/test_t06_tools.py tests/security/test_t06_tool_boundaries.py tests/integration/test_t07_research_graph.py tests/integration/test_t07_agent_semantics.py tests/integration/test_t07_golden_harness.py -q
```

实际汇总输出：

```text
47 passed in 117.08s (0:01:57)
```

覆盖 prompt 渲染与用户消息隔离、工具契约及权限边界、研究图正常提案、输出修复、预算与重复证据终止、Golden 回放。仅文字修改，未增加重复指令文本断言的测试。上述结果证明既有行为未回归，不证明真实模型调用序列改善。

验证期间并行任务修改了数据集、baseline 及相关测试（Golden harness 已断言 45 条）；这些改动不属于本任务。本次测试结果来自共享工作区，不能视为原始 30 条冻结状态上的独立验证。

## Live 与结论边界

本轮未运行 Live：仅检查进程环境中的 `MOONSHOT_API_KEY` 是否非空，结果为 False；未读取密钥文件。无优化后通过率。

复用既有报告的历史指标（每份 30 条）：

| 报告 | case_pass_rate | tool_sequence_accuracy |
| --- | ---: | ---: |
| `.artifacts/eval/scenario-a-live/kimi-k3-r5.json` | 30.00% | 53.33% |
| `.artifacts/eval/scenario-a-live/deepseek-pro-structured.json` | 33.33% | 63.33% |

Kimi 的提前终止用例记录为 `provider_failure`；仅凭现有报告不能将其归因于模型主动结束，也不能声称 prompt 已修复 provider 故障。

`src/oria/eval/scenario_a_live.py` 使用统一真实运行时、demo 规则和 local_operator，报告声明 `real_runtime_no_fixture_injection`。因此 missing_rule_category、permission_denied 和 duplicate_evidence 等 fixture 预期未必在 Live 环境真实触发。成功执行工具并不单凭预期序列差异就构成权限绕过。`src/oria/eval/scenario_a.py` 的 duplicate_evidence 回放固定重复 search；本次没有让模型为匹配该序列故意检索三次，没有修改 expected_tools 判定。

后续 Live 若使用并行任务更新后的数据集，须标明 dataset_sha256 和样本范围变化；不能直接将新旧汇总差异全部归因于 prompt。
