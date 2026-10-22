# T03 coupon reservation：实施前审查

2026-10-22；基线 c8100754817ed715eadc7d78da4630abccca92cb，master clean。先读 AGENTS.md、详细路线 649–659 及最新 T03 增量、ADR-025、上一卡 summary；仅阅读相关源码，不遍读巨型文档。本文件先于测试与源码修改保存。上一草案切片和既有验证卡保持不改写。

## ADR 核对与最小方案

- ADR-025 要求当前 requester、独立 worker、owner/epoch/有效 lease、实际资源 Policy，以及平台完整快照 no-op UPDATE 持锁到业务事务结束。本轮复用现有协议；不调用旧 domain.materialize_coupon_batch（它先 reserve 后 auth 且会 dispatch）。
- 新增显式内部 typed `JobExecutionService.reserve_coupon_materialization(token, args, plan, approval_id)`，固定 materialize_coupon_batch，不接受 Context/callback/SQL/自由 tool/adapter，也无默认 HTTP/Tool/Graph 装配。
- campaign/coupon/recruitment ID 均绑定 Job 派生值，业务事务取得 SQLite 写锁后读取完整持久草案，复用 CampaignDraft/hash 与 build_launch_plan 验证当前 approved plan 的全部内容、版本及 canonical child args。要求该 Job 已完成的草案账本成功凭据匹配，不能拿其他路径的草案冒充 Job 草案。
- checkpoint 只从受信 Business Repository 持久 saga 读取，限 planned（尚无物化），与真实 Platform approval checkpoint 严格相等。它是既有 saga 的逻辑审批点，不是 Job accepted saver pointer；本轮不接受 checkpoint 参数、不读写/推进 accepted pointer，不伪造 Graph 恢复。前置 approval/saga 仅由合成受信 fixture 经既有 ApprovalService/Repository 准备。
- 持平台锁后通过该事务读取 approval；锁阻止其他平台 writer 改变 approval/Job。小幅抽取原 ApprovalService 的纯 binding/expiry/status 判定，保留非 Job 原 invalidation/expiry 审计语义；Job 使用同一规则只读拒绝，并额外核对 requester/SOD、launch 无 downstream binding。实际 campaign launch Policy 与当前 policy_version 在 await 后及 COMMIT 入口重查，事务内不调用另开审计 writer 的 authorize。拒绝 Policy 沿既有 deferred audit 在解锁后记录。
- 业务单事务只在授权后 reserve 到既有 tool_executions：canonical_args_hash 与 ExecutionLedger.build_idempotency_key 使用旧 materialize 业务 scope。执行 ID 固定 Job/coupon 操作，防止同 Job 换内容新增第二笔；历史重用也先验当前 token/审批/事实。只允许 reserved，绝不标 executing/succeeded，不授予任何 dispatch 权限。
- 每个事务 await 后（关键写入之前及最终提交之前）复核身份/Policy/期限，COMMIT 入口同步复核。平台审批在锁内稳定、业务事实在业务写锁内稳定；时效在入口重新判断。

## 退出门禁

严格纵向 TDD：先新增一个真实行为 RED（已有可调用入口造成错误 reservation，而非 import error），保存原始日志；最小 GREEN 后逐项补齐有效路径、重复/跨 epoch 同稳定 key、换内容冲突、完整绑定、无效/拒绝/过期/撤销审批、身份/Policy 撤权、取消/到期/timeout/旧 epoch、await/COMMIT 变化回滚、异常/cancellation 释放锁与无部分账本。真实 SQLite 双资源实例竞争必须观测 contender 实际 DB UPDATE，避免只阻塞授权审计。新增函数均有行为覆盖。相关非 Job launch/approval/ledger/lease/draft 回归及受影响文件 Ruff/mypy；全量/独立安全/打包留给 orchestrator。

## 限制与不变项

只使用合成临时 SQLite；无新依赖/migration/环境变量、用户库/凭证/真实飞书/Live/Enterprise/Performance，无 git commit/checkout/push 或任何远程写入。业务库单库原子，不是跨库 2PC。平台锁阻塞其他写入，仍为 Community 单 worker；双实例不证明 PostgreSQL/S2/独立 worker。驱动接收 COMMIT 后的目录/Policy 撤权或时钟变化不能追溯撤销已提交事实；响应失败须读账本重试。launch 审批创建、派发/外部调用 fencing、Graph、其他领域、调度/恢复/Postgres S2 未实现；T02/T03/T04/Core 未关闭，V0.5-T07 29/80 不变。
