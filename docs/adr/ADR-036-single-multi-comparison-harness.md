# ADR-036：single/multi 公平对照 harness

- 状态：已接受
- 日期：2026-09-10
- 决策者：FrankLee
- 关联任务：V0.5-T05

## 背景

Supervisor 和两个专职 Subagent 已能运行，但控制流可运行不等于多智能体质量更好。如果 single 和 multi 使用不同的数据、模型、工具或预算，按固定顺序执行，向 judge 暴露架构身份，或只选择各自最佳结果，对照结论就不可复算且容易产生选择偏差。

V0.5-T05 只建立 Fixture/Community 可执行的公平方法和离线 harness。真实 Provider 对照、显著性判断与能力声明属于 V0.5-T07。

## 候选方案

1. 各架构独立调参并报告最佳一次：容易展示上限，但资源不等额、存在事后选择，不能支持架构价值判断。
2. 固定 single 后 multi 顺序并直接把结果交给 judge：实现简单，但有顺序效应和架构标签偏差。
3. 预注册同一 rubric，对冻结数据执行等额预算的成对重复；固定 seed 随机化全局顺序，通过盲评 seam 评分，最后揭示标签并报告全部运行和方差。

## 决策

采用方案 3：

- single 与 multi 必须使用完全相等的 `ComparisonBudget`。该预算复用 `NightlyBudget` 的 case、输入/输出 Token、成本与墙钟上限，并补充模型轮次、工具调用、总 Token 和相同的 per-case 终止限制。不相等的预算在运行前拒绝。
- 两侧复用 attribution golden v2、rubric v2、Fixture Runtime 配置、回放模型、工具注册表和终止规则。single 直接调用 `build_research_graph(spec=attribution_research_spec())`，multi 调用 `build_supervisor_graph()`。
- 调度表覆盖每个 case、每次 repetition 和两种架构，再由固定 seed 全局打乱。调度表随报告保存，可复算且不丢失顺序效应证据。
- rubric 在首个 run 前读取并绑定 SHA-256、criteria 和四类指标计划；全部 run 后再次校验。文件或预注册计划发生变化即拒绝报告。
- judge packet 只包含不透明 blind ID、rubric criteria 与回答内容，不含 architecture、Golden 标签、split、critical 或 provider profile。架构身份只在调度和报告聚合阶段揭示。
- 报告保留两种架构的所有 case/repetition 原始结果，分别记录质量、模型轮次、工具调用、输入/输出/总 Token、成本和端到端延迟，并计算样本方差。数据模型不提供 best-run 字段，不允许只保留最佳一次。
- Fixture 结果固定为 `descriptive_only`，不执行显著性推断。multi 质量差值为负时写 `multi_regressed`，为零时写 `mixed_or_equal`；只有质量差值实际为正才写 Fixture 范围内的提升，同时明确 Live 尚未验证。
- `eval compare` 默认只运行本地 Fixture。Community/Live 和 `--target` 在 T05 fail closed；T07 必须显式选择 Live target、真实模型和外部预算后再开放。

## 后果

- 正向影响：同一冻结资产下的 single/multi 对照可离线复算；顺序、盲评、预算和重复结果都有机器可检验的证据。
- 代价与局限：Fixture replay 主要验证 harness 与确定性行为，不代表真实模型表达质量；当前样本仅做描述性比较，不能宣称统计显著；multi 的 supervisor 开销必须计入同一总预算。
- 迁移/回滚：无数据库迁移和运行时依赖。可移除 compare 模块、CLI 子命令和 Fixture 预算配置，不影响既有 `eval run` 或 agent graph。

## 验证

- Contract：拒绝不等额预算；固定 seed 的执行顺序可复算且完整；judge schema 不含架构标签；rubric 注册后修改被拒绝。
- Integration：通过 `eval compare` 跑完整 attribution golden v2 Fixture，对两种架构保留全部 case run，报告质量、成本、延迟、工具/Token 和方差。
- 全量验证仅运行非 Live/Enterprise/Performance 与 security marker；T05 不发起真实网络请求。

## 关联资料

- [Oria 架构设计](../../Oria架构设计.md)：Eval 分层与指标分离。
- [Oria 详细执行路线](../Oria详细执行路线.md)：V0.5-T05、S1 与 8.3 核心测试。
- [ADR-009：多智能体边界](ADR-009-multi-agent-boundaries.md)。
- [ADR-015：Eval 子系统](ADR-015-eval-subsystem-and-gates.md)。
