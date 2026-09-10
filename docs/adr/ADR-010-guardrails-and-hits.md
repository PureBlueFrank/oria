# ADR-010：Guardrails、ABAC 与动态工具暴露

- 状态：已接受
- 日期：2026-09-10
- 决策者：FrankLee
- 关联任务：V0.5-T03

## 背景

V0.3-T02 已在 `LocalPolicyEngine` 中实现写 RBAC、职责分离、可信主体目录、跨 tenant 拒绝、确认任务分配和审计。但静态 `ToolRegistry` allowlist 仍会把全部已注册工具 schema 交给模型，且执行边界之前没有独立的 Guardrail 复核。同时，Prompt/RAG 注入和输出 PII/毒性缺少统一挂载点。

Prompt injection 检测不能成为授权系统：规则可能误报或漏报，不可以用检测结果授予或扩大权限。

## 候选方案

1. 只在工具执行时检查权限：执行可拒绝，但模型仍看到不可用工具，增加越权诱导与无效调用。
2. 让 Guardrail 自行解释角色和 ACL：接入快，但会产生第二权限源并与 PolicyEngine 漂移。
3. 保留静态注册 allowlist，模型调用前用 `ctx.policy` 生成动态子集，工具执行前再由 tool Guardrail 通过同一 PolicyEngine 重新鉴权；其他 Guardrail 只做内容安全。

## 决策

采用方案 3：

- `permission/` 是唯一鉴权源。`ToolAuthorizationGuardrail` 只接收 `AuthorizationRequest` 并调用 `ctx.policy.authorize`，不解释角色、ACL 或 Prompt/RAG 内容。
- `LocalPolicyEngine` 保留原 RBAC/职责分离判定顺序，并在通过后叠加 ABAC deny。`ResourceRef` 保持只表达资源身份；受信调用方可通过 `AuthorizationContext.attributes` 的 `organization/region/labels` 或 `resource_attributes` 表达资源作用域。organization/region 必须精确匹配，资源 labels 必须是主体 labels 子集；缺少、畸形或不匹配均拒绝。普通工具参数不自动解释为 ABAC 属性。
- 允许决策的 `constraints` 回传 tenant 与已验证属性作用域；拒绝决策不携带 constraints。策略版本升为 `local-v2`。
- 动态工具暴露逐个使用 `ToolPolicy.required_action/resource_type` 构造授权请求，仅向模型传递 allowlist 中当前允许的子集。静态 allowlist、注册完整性和 wheel verifier 契约不变。
- 工具执行在 schema/参数验证后运行 tool Guardrail 重新鉴权。因此模型可见集合建立后策略或主体属性变化，仍会在工具实现运行前 fail closed。
- `PromptInjectionGuardrail` 和 `RAGInjectionGuardrail` 均属于 `phase="input"`。命中只返回 `action="warn"` 和脱敏 reason code，可记录事件，不 block 正常请求、不改变指令优先级、不改变工具集合。RAG 实例作为 input 的一个具名来源挂载。
- `OutputSafetyGuardrail` 对邮箱、手机号、凭证和确定性毒性模式做递归脱敏，返回 `action="redact"`。`GuardrailResult.sanitized_content` 为带默认值的可选结果字段，保持旧值解析兼容。
- Community runtime 在 `ServiceRegistry[Guardrail]` seal 前注册 input prompt、input RAG、tool authorization 和 output safety 四个实例。

## 后果

- 正向影响：模型可见能力与实际执行权限共用同一决策源；属性或策略变化不会依赖旧的工具暴露结果；注入内容不能扩权。
- 代价与局限：工具暴露和执行会分别产生策略请求与审计量；确定性内容规则不是语义分类器，可能误报/漏报；当前电话脱敏聚焦明确手机号形态，避免破坏 ISO 日期和证据 ID。
- 迁移/回滚：无数据库 migration。回滚可移除 runtime 挂载和 agent/executor 调用；旧 checkpoint 不依赖新结果字段。

## 验证

- PolicyEngine 未知 action、缺少/不匹配资源属性、跨 tenant 和伪造角色均默认拒绝。
- 只读运营、活动管理员和审批人的动态工具子集与 PolicyDecision 一致。
- 暴露时允许、执行时策略变化的工具在 `run()` 前被 tool Guardrail 拒绝。
- 用户/RAG 注入只告警且不扩大工具集；输出 PII/凭证脱敏。
- `make lint`、非 Live/Enterprise/Performance 套件和独立 security 套件必须通过。

## 关联资料

- [Oria 架构设计](../../Oria架构设计.md)：权限单一来源、Guardrail Protocol 与 Prompt injection 安全边界。
- [Oria 详细执行路线](../Oria详细执行路线.md)：V0.5-T03、S4 和 8.3 核心测试。
- [V0.5-T03 验证卡](../../reports/verification/v0.5/20260910-t03/summary.md)
