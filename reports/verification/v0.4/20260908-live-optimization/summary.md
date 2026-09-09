# Scenario B Live 收口后加固与三轮复跑

- 日期：2026-09-09
- 起点：main / `855bc14`（决策规则整改后），起始工作区干净。
- 范围：V0.4-T03/T05 交付层与修复反馈的工程加固 + 冻结 holdout 三轮真实 Live 复跑；冻结数据集、rubric、baseline、target 与历史 failed 卡均未改动。
- 状态：本地门禁通过；三轮 Live 已运行，自动通过率稳定在约 75%，未宣称质量通过。

## 改动（一次工程加固）

- **可重试 provider 故障有界重试**：`graph.py` 对 `retryable` 的 `ProviderException`（限流/瞬时不可用/超时）在 `ResearchLimits.max_provider_retries`（默认 2，范围 0–5）内重试，不占用模型轮，每次重试记 `provider_retry` 审计事件；`StructuredOutputError` 恒为非可重试，继续走原修复路径。
- **决策规则/schema 校验反馈可执行**：`_decision_repair_guidance` 把原始 Pydantic 错误（support/refutation 不相交、保留全部 supported 候选、decision rules require 等）翻译成具体修复步骤，随 `validation_repair` 系统消息下发。
- **prompt v4**：在 v3 基础上新增决策契约与证据覆盖纪律——`evidence_indices` 与 `refutation_indices` 互不重叠、最终 hypotheses 逐条保留所有 supported 候选、因果追问须至少覆盖 `query_funnel` 与 `query_activity`，并给出索引用法正例。`prompt_version` 由 3 升为 4。
- **structured 输出 JSON 打捞**：`parse_structured_text` 先去除 Markdown 围栏、剥离最外层 JSON 前后散文、移除尾逗号，再解析；打捞结果仍过完整 schema 校验，不削弱验证。
- **finalization 关闭并行工具调用**：提交阶段 `parallel_tool_calls=False`，稳定 synthetic_tool 下单次保留提交，减少"须恰好一个保留提交"错误。
- **放宽 `evidence_indices` 为可选**（`min_length=1` → `default=()`）并补语义描述：ruled_out 候选不再被强制填支持索引，直接消除"同一观察同时作支持与反证"的根源；supported 候选的支持关系仍由 `evidence.supports` 与 hypotheses 对账校验兜底。

## 三轮 Live 结果（deepseek-pro-structured / deepseek-v4-pro，20 holdout × 3）

| 轮次 | 自动通过率 | outcome 准确率 | 必需工具覆盖 | 成本（上界） | run.json SHA-256 |
| --- | --- | --- | --- | --- | --- |
| r1 | 71.7%（43/60） | 71.7% | 100% | $1.64 | `0d80e591…312ac` |
| r2 | 76.7%（46/60） | 76.7% | 100% | $1.64 | `f00c6be6…93009` |
| r3 | 75.0%（45/60） | 75.0% | 100% | $1.64 | `518e754b…ddb4ea` |

对比基线：r4 归档（决策规则前）75.0%（含静默错答）；决策规则后首次 Live 73.3%。三轮结果在 71%–77% 间震荡，差异在随机噪声范围内。原始 `run.json`/`blind-review.json` 保存在本地 `.artifacts/eval/attribution-live-v2-r{1,2,3}/`（不入库），指纹如上。

## 失败分类（三轮合计 46 个失败 run）

- **决策契约四元对账**（`schema_validation_failed`/`decision_validation_failed`）：`retain every supported decision candidate`、`decision rules require conflicting/attributed`、`support and refutation distinct`、`candidate support must match evidence.supports`、`evidence references unknown hypothesis`——模型无法稳定对齐 candidates ↔ hypotheses ↔ evidence.supports ↔ outcome。
- **交付层**（`structured_output_error`）：synthetic_tool 下"非法 JSON / 保留提交数量错 / schema 不符"偶发。
- **模型行为**：幻觉工具名（`unknown_tool`，045）、伪造引用（`evidence_validation_failed`，043 r1）、语义误判（attributed↔conflicting/insufficient，自洽但错误，031/040/046/051）。

## 结论与边界

1. 加固本身有效且未引入回归：`make lint`/`make test` 878 项通过（4 条既有 SAWarning），`required_tool_coverage` 从 98.3% 稳定到 100%，provider 重试、JSON 打捞、可执行修复反馈均落地并被测试覆盖。
2. 自动通过率未能逼近 100%，卡在约 75% 的 **deepseek-v4-pro 能力天花板**：剩余失败是真实模型在严格决策契约上的推理/交付上限，非工程缺陷。ADR-033 的强制对账本意就是拦停静默错答，它正确地做到了，代价是把门槛抬到当前模型够不到的高度。
3. 不以重复运行挑选最好结果，不把 75% 表述为"接近 100%"或"质量通过"。自动门禁仍为 `completed_pending_human_review`，语义正确性须 ≥10 条去标签独立盲评；冻结 failed 卡与 V2 待盲评状态不变。
4. 若要真正突破，需另行决策：换更强模型，或简化决策契约（放宽 ADR-033 四元对账）——两者均属新架构/评测版本决策，不能以调 prompt/修复绕过。
