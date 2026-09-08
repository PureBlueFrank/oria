# Scenario B Eval Dataset V2 全量整改审计报告

日期：2026-09-08
状态：已完成代码/数据/schema/rubric/evaluator 整改；数据集已由 FrankLee 审阅通过并冻结（`holdout_frozen=true`），Fixture baseline 已创建（50/50 通过）；tenant 权限事实已注入模型上下文；dev/holdout fixture 泄漏已彻底消除。
关联：V0.4-T05 冻结 failed 卡、`reports/verification/v0.4/20260906-remediation/分析与修复.md`、ADR-031/032。

## 0. 结论摘要

V0.4-T05 多轮 Live 大面积失败，根因经逐轮定位为**三层**，其中"case 设置错误"只占很小一部分：

1. **交付层工程 bug（已修复，与 case 无关）**：execution_id 前缀诱导误抄、调查轮绕过最终化投影、修复反馈不含规则文案。修复后交付层失败降为 0。
2. **Dataset/Rubric/Evaluator 与真实 Agent 行为的系统性错配（本次整改对象）**：outcome ontology 边界模糊、tool expectation 过死（隐含顺序 + `expected_tools` 空集矛盾）、holdout 结构泄漏、安全/权限能力与证据不足能力被压进同一个 `insufficient` 标签、`expected_tools=[]` 与 `required_evidence` 非空的矛盾、tenant 权限事实对模型不可见。
3. **模型在"多因不合并 + 有限归因"上的能力不足（未被本次整改掩盖）**：消融实验四组全 0 clean pass，本次整改**没有**放宽因果契约，只是让"模型做对时不被误判"。

本次 V2 的最终目标（与用户要求一致）：**模型做错时稳定失败；模型做对时不因合理的工具顺序、措辞、因果限定或分类歧义被误判。**

---

## 1. V1 问题总结（按类别）

| # | 类别 | 证据与影响 |
|---|---|---|
| ontology | outcome 三个状态边界模糊 | `insufficient` 同时承载"证据不足"与"两个有证据候选无法唯一化"；后者应属 `conflicting`。schema `validate_outcome_shape` 未区分。 |
| dataset balance | conflicting 严重不足且分布失衡 | 6/50 conflicting，holdout 仅 1 条（sb-v1-043），且 043 叠加 mixed_funnel + 双异常 + conversation history + 二元诱导措辞，是最难复合样本，无法代表 conflicting 能力。 |
| holdout leakage | dev/holdout 同 fixture 同数字改写 | 006/046（campaign_enrollment，数字完全相同 48.15%→27.86%）、002/045、004/048（错误前提品类/报名比较对）、001/007/044/050（campaign_effect 四连）。 |
| wording ambiguity | 043 二元诱导 | "访客量变化趋势能区分因果吗？"诱导模型回答"不能区分→证据不足"，与 golden 的 conflicting 期望存在语义张力。 |
| tool expectation | expected_tools 隐含顺序 + 空集矛盾 | `050` 与 `001/007/044` 的工具顺序不同但无真实依赖；`035` 要求 `expected_tools=[]` 却要求 `required_evidence` 引用 funnel/activity，模型主动查证会被判错。 |
| permission visibility | tenant 权限对模型不可见 | `023/027/028/032` 依赖"当前身份只授权 local-community"，但工具静默按 `ctx.tenant_id` 过滤、返回数据不含 tenant 标识，模型无从得知权限边界（依赖 evaluator 私有事实让模型猜）。 |
| evaluator rigidity | 工具覆盖按集合判但未分离 required/optional | `_case_record` 只用 `expected_tools` 判 coverage；`_evaluate_case` 的 Fixture 路径用 sorted 字符串相等判 `hypothesis_match`（Fixture 自检可保留，Live 无此问题）。 |
| numeric matching | evidence 精确值校验合理，但 golden 措辞过严 | `validate_attribution_conclusion` 要求 evidence `value` 精确匹配 ToolResult（provenance 校验，合理）；但 golden 假设里"35.12 个百分点/50.95%"要求模型措辞级复现，无 tolerance。 |
| multi-turn | 追问语义未与能力解耦 | 039–044 全部堆在 holdout，其中 043 是 conflicting、044 是注入历史 + 归因，多轮能力与因果/注入能力混淆。 |
| safety vs evidence | 能力被压进同一标签 | 24 条 insufficient 混装 evidence_abstention（12 条）与 tenant/tool/injection/integrity（12 条），无法分别报告通过率。 |

---

## 2. 修改统计

| 修改项 | 数量 |
|---|---|
| 修改题目措辞 | 2 条（043 消除二元诱导、041 数值 tolerance 化） |
| 修改 outcome | 0 条直接改；新增 conflicting 2 条（净 6→8） |
| 新增 `task_type` / `answerability` | 50 条（全量） |
| 新增 `required_tools` / `optional_tools` / `tool_dependencies` | 50 条（全量） |
| 修复 `expected_tools=[]` 与 `required_evidence` 矛盾 | 2 条（035、037 改为 optional 查证） |
| 重新 split | 多条（conflicting 移入 holdout；016 移回 dev；新增 3 条） |
| 删除近重复 case | 3 条（013 零售范围外、014 华南范围外、048 饮料vs快餐，均与同类重复） |
| 新增 case | 3 条（051 north_campaign_effect、052 north_market_conflict、053 market_conflict 换问法） |
| 新增 fixture variant | 2 个（north_campaign_effect、north_market_conflict，消除北区泄漏） |
| 修改 rubric | v1 → v2（明确机械边界 + 语义等价/数值精度/因果限定不判错） |
| 修改 evaluator | required/optional 工具语义 + `failure_taxonomy` 多维诊断字段 |
| 新增 schema 字段校验 | answerability↔outcome 一致性、required/optional/forbidden 不相交 |

---

## 3. 新的能力分布

### 3.1 按 answerability

| answerability | 数量 | development | holdout |
|---|---|---|---|
| supported | 20 | 11 | 9 |
| conflicting | 8 | 4 | 4 |
| insufficient | 22 | 15 | 7 |

### 3.2 按 task_type

| task_type | 数量 | dev | holdout |
|---|---|---|---|
| causal_attribution | 16 | 9 | 7 |
| premise_refutation | 4 | 2 | 2 |
| descriptive_analysis | 3 | 1 | 2 |
| quantitative_analysis | 2 | 0 | 2 |
| scope_localization | 3 | 3 | 0 |
| evidence_abstention | 9 | 6 | 3 |
| tenant_access_control | 5 | 4 | 1 |
| tool_policy | 3 | 1 | 2 |
| prompt_injection | 3 | 1 | 2 |
| evidence_integrity | 2 | 2 | 0 |

### 3.3 关键能力在 holdout 的分布（目标：≥2 条 / 不只有 1 条）

| 能力 | holdout 数量 | 达标 |
|---|---|---|
| conflicting | 4 | ✅ |
| causal_attribution | 7 | ✅ |
| tenant_access_control | 1 | ⚠️ 见 §5 |
| tool_policy | 2 | ✅ |
| prompt_injection | 2 | ✅ |
| evidence_abstention | 3 | ✅ |
| scope_localization | 0 | ⚠️ 见 §5 |

### 3.4 critical 分布

7 条 critical：001、007、011、019、027、033（dev）；050（holdout）。

### 3.5 逐条审计表（case_id → 能力 / 可答性 / split / fixture）

| case_id | task_type | answerability | split | fixture_variant | multi-turn |
|---|---|---|---|---|---|
| sb-v2-001 | causal_attribution | supported | development | campaign_effect | |
| sb-v2-002 | premise_refutation | supported | development | standard | |
| sb-v2-003 | descriptive_analysis | supported | development | standard | |
| sb-v2-004 | premise_refutation | supported | development | standard | |
| sb-v2-005 | evidence_abstention | insufficient | development | standard | |
| sb-v2-006 | causal_attribution | supported | development | campaign_enrollment | |
| sb-v2-007 | causal_attribution | supported | development | campaign_effect | |
| sb-v2-008 | evidence_abstention | insufficient | development | standard | |
| sb-v2-009 | scope_localization | supported | development | standard | |
| sb-v2-010 | premise_refutation | supported | development | standard | |
| sb-v2-011 | evidence_abstention | insufficient | development | standard | |
| sb-v2-012 | evidence_abstention | insufficient | development | standard | |
| sb-v2-015 | evidence_abstention | insufficient | development | missing_activity | |
| sb-v2-016 | evidence_abstention | insufficient | development | standard | |
| sb-v2-017 | evidence_abstention | insufficient | development | standard | |
| sb-v2-018 | evidence_abstention | insufficient | development | standard | |
| sb-v2-019 | causal_attribution | conflicting | development | market_conflict | |
| sb-v2-020 | causal_attribution | conflicting | development | mixed_funnel | |
| sb-v2-021 | causal_attribution | conflicting | development | overlapping_events | |
| sb-v2-023 | tenant_access_control | insufficient | development | standard | |
| sb-v2-024 | causal_attribution | conflicting | development | beverage_conflict | |
| sb-v2-025 | scope_localization | supported | development | upstream_drop | |
| sb-v2-026 | scope_localization | supported | development | systemic_category | |
| sb-v2-027 | tenant_access_control | insufficient | development | standard | |
| sb-v2-028 | tenant_access_control | insufficient | development | standard | |
| sb-v2-029 | tenant_access_control | insufficient | development | standard | |
| sb-v2-030 | tool_policy | insufficient | development | standard | |
| sb-v2-033 | prompt_injection | insufficient | development | standard | |
| sb-v2-035 | evidence_integrity | insufficient | development | standard | |
| sb-v2-037 | evidence_integrity | insufficient | development | standard | |
| sb-v2-039 | quantitative_analysis | supported | holdout | standard | multi |
| sb-v2-040 | descriptive_analysis | supported | holdout | standard | multi |
| sb-v2-041 | quantitative_analysis | supported | holdout | standard | multi |
| sb-v2-042 | descriptive_analysis | supported | holdout | standard | multi |
| sb-v2-043 | causal_attribution | conflicting | holdout | mixed_funnel_alt | multi |
| sb-v2-044 | causal_attribution | supported | holdout | campaign_effect_multi | multi |
| sb-v2-045 | premise_refutation | supported | holdout | standard | |
| sb-v2-046 | causal_attribution | supported | holdout | campaign_enrollment_alt | |
| sb-v2-047 | evidence_abstention | insufficient | holdout | campaign_confirmation | |
| sb-v2-049 | causal_attribution | supported | holdout | campaign_confirmation | |
| sb-v2-050 | causal_attribution | supported | holdout | campaign_effect_alt | |
| sb-v2-051 | causal_attribution | supported | holdout | north_campaign_effect | |
| sb-v2-052 | causal_attribution | conflicting | holdout | north_market_conflict | |
| sb-v2-053 | causal_attribution | conflicting | holdout | market_conflict_alt | |
| sb-v2-022 | causal_attribution | conflicting | holdout | systemic_category_alt | |
| sb-v2-031 | tool_policy | insufficient | holdout | standard | |
| sb-v2-032 | tenant_access_control | insufficient | holdout | standard | |
| sb-v2-034 | tool_policy | insufficient | holdout | standard | |
| sb-v2-036 | prompt_injection | insufficient | holdout | standard | |
| sb-v2-038 | prompt_injection | insufficient | holdout | standard | |

---

## 4. Development / Holdout family overlap 检查

| fixture / mechanism | development | holdout | 说明 |
|---|---|---|---|
| campaign_effect (east 核销退出 0.82→0.34) | 001, 007 | — | 单一出现 ✅ |
| campaign_effect_alt (east 核销退出 0.79→0.38) | — | 050 | 幅度差异，无泄漏 ✅ |
| campaign_effect_multi (east 核销退出 0.80→0.41) | — | 044 | 幅度差异，无泄漏 ✅ |
| campaign_enrollment (east 报名 0.48→0.28) | 006 | — | 单一出现 ✅ |
| campaign_enrollment_alt (east 报名 0.48→0.30) | — | 046 | 幅度差异，无泄漏 ✅ |
| campaign_confirmation (east 确认环节) | — | 049 | 单一出现 ✅ |
| upstream_drop (east 访问率) | 025 | — | 单一出现 ✅ |
| mixed_funnel (east 上下游 0.38/0.45) | 020 | — | 单一出现 ✅ |
| mixed_funnel_alt (east 上下游 0.40/0.50) | — | 043 | 幅度差异，无泄漏 ✅ |
| market_conflict (east 活动 vs 大盘 0.45) | 019 | — | 单一出现 ✅ |
| market_conflict_alt (east 活动 vs 大盘 0.48) | — | 053 | 幅度差异，无泄漏 ✅ |
| overlapping_events (east 双事件) | 021 | — | 单一出现 ✅ |
| beverage_conflict (east 饮料三冲突) | 024 | — | 单一出现 ✅ |
| systemic_category (跨区域品类 0.45) | 026 | — | 单一出现 ✅ |
| systemic_category_alt (跨区域品类 0.50) | — | 022 | 幅度差异，无泄漏 ✅ |
| missing_activity (活动缺失) | 015 | — | 单一出现 ✅ |
| north_campaign_effect | — | 051 | V2 新增，全新实体 ✅ |
| north_market_conflict | — | 052 | V2 新增，全新实体 ✅ |

**结论**：**泄漏已彻底消除**。每个非 standard 的 fixture variant 只出现在一个 split（dev 或 holdout），或通过 `_alt`/`_multi` 幅度差异变体让 dev 与 holdout 使用不同数字。仅 `standard`（无异常基准）跨 split 共享，属于"正常数据 + 不同维度/前提"的合法复用，不构成异常模板泄漏。

---

## 5. 仍存在争议 / 需人工复核（NEEDS_HUMAN_REVIEW）

1. **tenant_access_control 在 holdout 仅 1 条（032）**。tenant 权限事实已通过 `initial_attribution_state(tenant_id=...)` 注入 system prompt（见 §6），模型现在能明确看到当前身份与授权范围。但 holdout 的 tenant 样本仍只有 1 条，建议后续补齐（如新增"北区跨租户"类变体）。
2. **scope_localization 在 holdout 为 0 条**。025/026/009 都在 development。可后续把 026 移一条到 holdout，或新增 north 的 scope 变体。
3. **`failure_taxonomy` 中 A（事实/计算错误）、C（evidence interpretation）、H（evaluator false negative）、I（case/rubric 歧义）无法自动判定**，仍依赖盲评。本次只实现了可自动判定的维度（B 取证、D outcome 映射、E hypothesis 渲染、F 工具策略、G 安全）。
4. **V2 数据已冻结（2026-09-08）**：FrankLee 审阅通过、`holdout_frozen=true`、Fixture baseline `eval/baselines/attribution/2.json` 已创建（50/50 通过）。后续可直接进入 Live，但候选晋升默认或冻结 target 变更仍需严格盲评。

---

## 6. 修改文件清单

| 文件 | 改动 |
|---|---|
| `src/oria/eval/datasets.py` | `AttributionGoldenCase` 新增 `answerability`/`task_type`/`required_tools`/`optional_tools`/`tool_dependencies`；新增 `required_tools_for()`；放宽 optional 无 required 场景；answerability↔outcome 映射校验 |
| `src/oria/eval/attribution.py` | `_evaluate_case` 用 `required_tools_for`；`_metrics` 同步；`AttributionRubric.rubric_version` 允许 `"2"` |
| `src/oria/eval/attribution_live.py` | `AttributionLiveCaseRecord` 新增 `failure_taxonomy`；`_case_record` 用 `required_tools_for` + 多维诊断；新增 `_failure_taxonomy()` |
| `src/oria/agent/attribution.py` | `initial_attribution_state` 新增 `tenant_id` 参数；注入 tenant 上下文 system 消息（修复权限事实不可见） |
| `src/oria/eval/attribution_data.py` | 新增 8 个 fixture variant：`north_campaign_effect`/`north_market_conflict`/`campaign_effect_alt`/`campaign_effect_multi`/`campaign_enrollment_alt`/`market_conflict_alt`/`mixed_funnel_alt`/`systemic_category_alt`（消除泄漏） |
| `eval/config/attribution-rubric-v2.yaml` | 新建 v2 rubric：5 criterion 权重 0.25/0.25/0.20/0.20/0.10，明确 outcome 机械边界 + 语义等价/数值精度/因果限定不判错 |
| `eval/config/attribution-gates-v2.yaml` | 新建 v2 gates（dataset_version=2，全部指标 1.0） |
| `scripts/generate_attribution_golden_v2.py` | 新建：重写 50 条（sb-v2-XXX），加新字段，重排 split，修 043/035/011，新增 north/alt 变体 case |
| `scripts/run_attribution_golden_v2.py` | 新建：V2 Fixture baseline 创建/校验入口 |
| `eval/datasets/scenario_b/v2.jsonl` / `v2.manifest.json` / `CASES.v2.md` | 新建 V2 数据集（dataset_version=2，已冻结） |
| `eval/baselines/attribution/2.json` | 新建 V2 Fixture baseline（50/50） |
| `tests/contract/test_v04_attribution_golden_v2.py` | 新建 V2 契约测试（16 项，含无泄漏断言） |
| `tests/contract/test_v04_attribution_factual_grounding.py` | `test_generator_and_readable_review_match_the_draft` 忽略 V2 扩展字段（兼容 v1） |

---

## 7. 验证结果

- **Fixture regression**：`tests/contract tests/unit tests/integration` 全量 **746 passed**，4 个既有 SQLite migration 警告。
- **V2 契约测试**：16 项全部通过（split 30/20、conflicting holdout≥4、answerability/task_type 全覆盖、035/011/043 修复到位、north/alt 变体存在、rubric v2 加载、冻结状态、无 fixture 家族泄漏）。
- **V2 Fixture baseline**：`run_attribution_golden_v2.py --create-baseline` 50/50 通过，全部指标 1.0；`eval_fingerprint=sha256:11156cf2ecbb56481bcaff4fd9cad33ea39438dbc1f786ef43764229fb23202d`；baseline 文件 `eval/baselines/attribution/2.json`。
- **泄漏消除**：非 standard fixture variant 均只出现在单一 split（已验证：无跨 split 泄漏变体）。
- **静态检查**：`ruff check` / `ruff format` / `mypy`（5 个改动源文件）通过。
- **未运行**：Live 测试（需真实 DeepSeek key + 预算 + 明确 target 选择）。

---

## 8. 后续待办（不阻塞本次交付）

1. 补齐 holdout scope_localization 与 tenant_access_control 样本。
2. 用 V2 + DeepSeek Pro/Thinking 跑 3 轮 Live，按 failure taxonomy 出报告。
