# ADR-040：外部副作用接纳、幂等与未知结果对账

- 状态：已接受（类型契约与本地 conformance fixture）；真实 Adapter 实现及验收未完成
- 日期：2026-10-22
- 关联：V0.6-T03/T04、ADR-025、ADR-029

## 背景与范围

现有 `CouponBatchAdapter` 不消费 Job fencing token。`JobCouponDispatchService` 因此继续只接受精确的 `InMemoryCouponBatchAdapter`，不能将网络 Adapter 接到持 SQLite 锁的 Mock 调用窗口。仅在发请求前检查一次租约，或者让接收端记住“目前见过的最大 epoch”，都不能拒绝新 epoch 尚未到达接收端时的旧 worker 请求。

新增 `oria.core.dispatch`，定义 `DispatchOperation`、`DispatchRequest`、`DispatchAdmission`、`DispatchObservation` 和泛型 `FencedDispatchAdapter`，以及绑定校验和纯对账投影函数。本契约没有生产接收端、认证传输、持久账本或默认运行时装配；不扩大当前 Job 派发入口。

## 决策

1. 稳定去重键为 `(tenant_id, adapter_id, tool_name, operation_key)`。payload hash、Job ID 和 lease epoch 不进入去重键；否则改参数或接管可能制造第二个副作用。首次接纳将完整 operation（含 Job、资源引用、schema version、canonical args hash）与稳定键持久绑定；同键任何绑定变化均拒绝。参数哈希复用审批的规范化算法，接收端还须独立验证具体业务资源与参数的对应关系。
2. 接纳携带 tenant/job/owner/epoch，但 token 自身不授予权限。接收端须认证调用方，检查当前 requester/独立 worker Policy、审批、领域事实，并把**权威当前租约检查与持久接纳线性化**。租约过期、Job 取消、错误 owner/epoch 或撤权必须拒绝，包括返回历史的请求。由 worker 自报的 Job 快照不构成权威；`validate_dispatch_admission` 只是原子边界内的纯校验器。
3. 真实实现必须说明其事务/网关如何序列化接纳与租约变更，并在最终副作用边界落实稳定键。远程调用前的一次查询、进程内锁或高水位缓存都不能代替该证明。若系统无法提供此边界，保持关闭，不宣称外部 fencing。已在有效租约内持久接纳的操作不会因之后接管自动撤销；后继 epoch 读取同一操作历史，不创建第二次效果。
4. `admit` 返回已接纳的 pending、succeeded、rejected 或 unknown；不能返回 not_found。pending 只证明接纳，不证明完成。`DispatchRejected` 表示本次接纳请求被拒绝，不证明此前尝试没有生效。观察结果中的 rejected 则是已绑定操作的确定终态，必须有对应的 rejected receipt，才能将执行账本投影为 failed。
5. `lookup` 是经过鉴权的只读查询，不接纳、不重试。超时、断连、异常、pending、unknown 和 not_found 均映射为 unknown；not_found 可能是查询可见性或尚在途的问题，不能清空未知账本或授权重投。只有与完整 operation 绑定、来自已认证目标的确定 receipt，才能将 unknown 投影为 succeeded/failed。`reconcile_dispatch` 不写领域库或 ledger；原子提交、补偿与人工处理仍需后续实现。
6. 公共 helper 严格重验完整嵌套模型，拒绝 `model_copy/model_construct` 绕过及隐式类型转换；不能只校验一个归一化副本后，让调用方继续使用另一种原始键。不得因异常输出或序列化警告记录原始业务参数。契约只携带稳定引用、hash、状态和脱敏 receipt。

## 验证与后果

契约测试的内存 receiver 在同一锁内修改合成租约权威和接纳历史，覆盖旧 epoch 先到、重复/跨 epoch、同键冲突、身份/取消/过期、响应丢失、只读查询和无凭据终态拒绝。它只是可执行的 conformance 示例，不是可复用生产 Adapter，也不证明跨进程、持久化或企业系统安全。

每个真实 Adapter 必须另存目标、身份、原子接纳方案及故障/接管/重放证据。该卡通过前，不启用网络派发，不关闭 T03/T04/Core 或 PostgreSQL S2 门禁。见[本地契约验证卡](../../reports/verification/v0.6/20261022-t03-dispatch-contract/summary.md)。
