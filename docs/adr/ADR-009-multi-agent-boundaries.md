# ADR-009：多智能体边界

- 状态：已接受
- 日期：2026-09-10
- 决策者：FrankLee
- 关联任务：V0.5-T04

## 背景

Oria 已有唯一的有界 `research_agent` model/tool/validate 循环，并用 `ResearchSpec` 为场景 A 招商和场景 B 归因替换 Prompt、工具、预算与结果 schema。引入多 Agent 后，需要在不复制该循环、不让模型改写安全边界、不放大调用方权限的前提下选择专职 Subagent。

自由 LLM supervisor 会使路由取决于非确定性输出，难以断言、重放和审计，也可能将 Prompt 内容误当成授权信号。Subagent 失败如果由 supervisor 静默重试或任意改派，还会导致无限循环或用错误领域结果掩盖失败。

## 候选方案

1. LLM 自由选择 Subagent 并动态改写计划：灵活，但路由与安全边界不可确定重放。
2. 为每个 Subagent 复制 model/tool/validate 循环：短期隔离直观，但终止、修复、工具失败和 checkpoint 语义会漂移。
3. 使用确定性 tool-based supervisor 路由到由 `ResearchSpec` 绑定的共享研究子图，以固定 handoff/result schema、权限交集、循环上限和显式失败回收约束编排。

## 决策

采用方案 3：

- Supervisor 使用固定短语规则分类，不调用 LLM 解析意图。归因特征先于招商特征，因此“招商转化率为什么下降”稳定路由到归因 Subagent。未匹配请求显式终止为 `unsupported_request`。
- 专职 Subagent 固定为 `campaign_research` 和 `attribution_research`，分别绑定现有 `campaign_research_spec()` 与 `attribution_research_spec()`。两者只能经 `build_research_graph(spec=...)` 构建，不复制或分叉 model/tool/validate 循环。
- `SupervisorHandoff` 与 `SubagentResult` 是严格、带 schema version、可 JSON 序列化的跨图值。Supervisor 计数、handoff、活动委托、结果、终止和事件都存于 LangGraph state，不用进程内可变计数器。每次子图 handoff 使用基于 run、序号和 Subagent 名的独立 checkpoint namespace。
- Subagent 原样继承同一 `ctx`，actor、executor、tenant 和 policy 均不替换。模型可见工具是 `ResearchSpec.tool_names` 与调用方在原 `ctx.policy` 下授权集合的交集；执行前仍由 T03 tool authorization Guardrail 通过同一 policy 重新鉴权。Supervisor 不解释角色，也不授予工具。
- `DEFAULT_MAX_HANDOFFS=2` 是不可超过的上限；测试可以降低 state 中的有效上限，不能提高。新 handoff 超限前终止为 `max_handoffs_exceeded`。
- Subagent 返回 `failed` 或 `waiting` 时，Supervisor 保存原 termination 与 usage，记录 `subagent_recovered` 事件并以对应状态显式终止上报。当前两个领域 Subagent 不可互换，因此不在领域间盲目改派或重试。
- 只有当可断言的确定性规则无法表达任务拆分，且已有独立评测、等额预算、隐藏架构标签和新 ADR 时，才考虑 LLM 计划/编排；它不得改写工具 allowlist、鉴权、预算、终止或人工审批边界。

## 后果

- 正向影响：路由可断言、可重放和可审计；两个专职 Subagent 共享成熟的有界循环；任何 handoff 都无法扩大调用方能力；失败和等待状态不会被吞掉。
- 代价与局限：固定短语路由的覆盖面有限，新领域需显式增加路由契约与 spec；权限在暴露和执行阶段均判定，会增加审计量；归因工具未在 Community `build_runtime` 的默认 ToolRegistry 中挂载时，归因 handoff 会 fail closed 为 `subagent_tools_unavailable`，而不会暴露或伪造能力。
- 迁移/回滚：无数据库 migration，旧 checkpoint 不会被当作 supervisor state 加载。可从 `ctx.agents` 移除 `supervisor` 注册回滚，既有 `research_agent` 与 `scenario_a` 注册语义不变。

## 验证

- CT 断言 handoff/result JSON 契约、重叠短语路由、不支持请求、handoff 上限和 failed termination 回收。
- E2E-F 在 Mock Provider、本地 SQLite 和 Fixture 规则下跑通 supervisor → `campaign_research` → 共享研究子图 → 结果回收。
- SEC 断言 Subagent 可见集同时是 spec allowlist 和原调用方授权集合的子集，额外 supervisor 工具不可见，未授权工具在 `run()` 前被拒绝。
- `make lint`、非 Live/Enterprise/Performance 套件和独立 security 套件必须通过。

## 关联资料

- [Oria 架构设计](../../Oria架构设计.md)：唯一有界 research agent 原语、Subagent seam 与 Planner 安全边界。
- [Oria 详细执行路线](../Oria详细执行路线.md)：V0.5-T04 与 8.3 核心测试。
