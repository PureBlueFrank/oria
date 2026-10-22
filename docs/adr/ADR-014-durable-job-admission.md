# ADR-014：Durable Job 受理与状态存储

- 状态：已接受；受理切片已实现，完整 V0.6-T03 仍在实施
- 日期：2026-10-22
- 关联：V0.6-T03；架构 §3.8、ADR-003、ADR-029、ADR-037

## 决策

会话与 Job 写入 platform 数据库。HTTP 只创建会话、受理结构化任务、查询和请求取消；不在 FastAPI 后台执行 Graph。默认关闭，通过服务端 `job_service: true` 显式装配，沿用 `approval_runtime_config` 指向的同一 RuntimeServices、Policy 与数据库生命周期。目前启动配置仅允许 Mock/Fixture 与 SQLite，启动只读核验 schema，不自动迁移。

一次提交支持 `campaign` 或 `attribution` 的有界类型化槽位。保存规范 JSON 及 SHA-256，拒绝额外字段；不保存原始自然语言、bearer、JWT claims 或角色快照。`requester_subject_id` 加原始 issuer/subject 作为稳定主体引用供后续 worker 重载身份。租户、所有者、job/thread/run ID、attempt、checkpoint 与 lease 均由服务端生成或维护。会话的 tenant/所有者/原始身份通过复合外键绑定到 Job。

`Idempotency-Key` 为 1–128 个 ASCII 字母、数字或 `._:-`，仅保存 SHA-256。唯一范围为 `(tenant_id, requester_subject_id, key_hash)`；规范提交内容包括 session。相同键和内容返回已有 Job 的当前状态，包括重启后重试；相同键换内容返回 409。并发写使用数据库唯一约束和 INSERT ON CONFLICT，创建事件只记录一次。相同内部主体的不同受信通道映射共享范围，不因切换 issuer 获得另一份业务执行资格。

会话创建与 Job 权限新增受限 action，继续要求受信 actor/executor 和 tenant。读、提交与取消必须是资源所有者；场景提交另检查已有 `campaign:draft:write` 或 `analytics:read`，不把获准排队等同于后续业务执行授权。每个异步边界重查身份，事务提交前重评当前 Policy。事务内拒绝先回滚，再由原 AuditService 写拒绝审计，避免 SQLite 第二个写连接争锁。授权审计记录的是授权决定；不作为业务已提交的凭证。提交的事实以 Job 与事件为准。

状态沿用架构 §3.8，增加单调 `revision`。Repository 验证类型、不变量、允许的状态边、不可变绑定和 attempt/epoch 计数，并以完整快照 CAS 更新。状态更新与对应 `job_events` 在同一事务内提交或回滚；sequence 从 0 开始，等于 revision。事件只保存状态、事件类型、序号及时间，不保存业务正文或身份凭证。该存储事件尚未冻结为 T05 的 SSE union，也不提供公开事件流。

排队、等待和计划重试可以直接进入 `cancelled`；运行中进入 `cancellation_requested`，等待持有执行权的 worker 确认停止。重复取消返回当前取消状态，成功或失败终态不能复活。取消不能撤销已提交副作用。等待态必须具备完整 accepted checkpoint 指针，但本增量只验证内部受信状态转换，未接通审批/外部事件到 Job 的恢复路径。

## 迁移与验证

新增 `platform_0013` 的 `sessions/jobs/job_events`，保留既有审批数据和旧 migration 摘要。新表具备 tenant 复合键、所有者外键、幂等唯一键、状态与预算约束；PostgreSQL 启用 FORCE RLS。降级仅允许三张表均为空，存在任一会话或任务数据时拒绝。PostgreSQL 降级须在独占锁内证明具有不受 RLS 过滤的读取能力，否则拒绝，不能把当前租户的空视图当成全库为空。`row_security_active` 的语义见 [PostgreSQL 17 文档](https://www.postgresql.org/docs/17/functions-info.html)。

本地验证使用临时 SQLite、真实 JWT/Policy、并发、事务故障注入、独立解释器和应用重启，见[验证卡](../../reports/verification/v0.6/20261022-t03-job-admission/summary.md)。PostgreSQL DDL/SQL 已有实现，本 revision 尚未进行真实 PostgreSQL 验证。历史 PostgreSQL 或飞书通过卡不外推到新表或任务执行。

后续继续按 T03 接入现有两场景服务和 ADR-029 事件恢复，按 ADR-025/T04 实施 lease claim、heartbeat、timeout、fencing 与 worker。当前预留 lease/预算/checkpoint 列及 CAS 不构成 worker 接管保证；默认服务中没有执行器消费 queued Job。

2026-10-22 后续：`job_inputs` 已提供无副作用的两场景初始 state 适配，逐项验证 owner/标识、初始状态和快照摘要。招商必须显式提供完整服务端业务模板，业务 ID/幂等键从 Job 派生，策略有效期不得超过 Job deadline；归因复用原 ResearchSpec。原始聊天不进入初始 state。输入 E2E-F 通过不授予执行权；当前身份恢复、lease/fencing 与受控调度仍须后续实施，见[独立验证卡](../../reports/verification/v0.6/20261022-t03-graph-inputs/summary.md)。

2026-10-22 租约增量：已按 ADR-025 实施当前身份恢复、独立 worker 授权、Job 行 claim/heartbeat/finish 与 accepted pointer CAS。每次 claim 递增 epoch，正常等待续行不消耗失败尝试预算。领域提交/副作用 fencing 与实际 runner 仍未接入；此前基线中的“未实现 claim”保留历史语境，当前范围见[租约验证卡](../../reports/verification/v0.6/20261022-t03-job-leases/summary.md)。
