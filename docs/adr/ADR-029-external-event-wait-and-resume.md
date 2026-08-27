# ADR-029：外部事件等待与可验证恢复

- 状态：已接受
- 日期：2026-08-27
- 关联任务：V0.3-T02、V0.3-T05、V0.3-T06、V0.6-T03

## 背景

招商报名窗口和选品回执可能跨小时或跨天，不能由一次 LLM 调用、进程内任务或客户端直接 resume checkpoint 承载。外部事件还存在重复、乱序、伪造、迟到和版本竞争。

## 决策

1. Graph 以 `ExternalWaitState` 进入 `waiting_event`，wait 必须绑定 tenant、event type、resource、expected version、checkpoint、关联 token hash、过期时间和 timeout action。
2. Adapter 从受限 `InboundRequest` 验签、防重放并做可信主体/租户映射，只产生标准化 `IntegrationEventEnvelope`。
3. `integration_event_inbox` 以 `(tenant_id, adapter_id, source_event_id)` 去重；事件必须先入 inbox，再以 CAS 解析唯一匹配 wait 并将 Job 置回 queued。
4. 事件最小 union 为 `merchant.enrollment_upserted / enrollment.window_closed / selection.decision_recorded / selection.completed`；未知类型不得直接恢复 Graph。
5. 报名窗口关闭后的迟到事件默认拒绝且保留原因；如冻结规则明确允许补报，则建立新业务版本并使下游审批失效。
6. 客户端和外部系统不能提供 checkpoint ID、wait ID 或权限 claims；worker 从受信状态恢复并重新鉴权。

## 后果

- 等待可跨进程和跨天恢复，且每次恢复都可追溯到已验签事件。
- inbox 是外部事件接受事实，Checkpoint 仍是执行恢复真相源，两者不互相代替。
- 必须维护 Adapter 事件映射、资源版本规则和 inbox 保留策略。

## 验证

- 重复、乱序、过期、迟到、错误版本、错误资源、未授权和无匹配 wait 事件不能推进 Job。
- 事件入 inbox 后崩溃，新 worker 可幂等继续解析，不重复执行前序副作用。
- `merchant / auto / hybrid` 三种模式、窗口关闭 join 和选品完成事件均有 Graph-Resume CT/REC。
