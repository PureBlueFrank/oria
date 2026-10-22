# ADR-025：Job lease 与 fencing 恢复

- 状态：已接受（身份恢复、Job 租约及 SQLite 草案/券 reservation 切片）；其余领域/外部副作用、runner 和接管验收仍待实施
- 日期：2026-10-22
- 关联：V0.6-T03/T04、ADR-004、ADR-014

## 实施约束

worker 必须是独立 executor service identity，恢复 requester 时重新读取当前主体属性并鉴权，不能复用提交时的 bearer 或角色快照。数据库原子 claim 递增 attempt 与 lease_epoch；heartbeat、状态、accepted checkpoint 推进及副作用 reservation 均须校验当前 owner/epoch 与有效期。SQLite 维持单 worker，PostgreSQL 竞争领取及跨进程恢复须独立验证。

每个 epoch 使用独立 checkpoint namespace；新的 attempt 只从 Job 指向的 accepted checkpoint 恢复。旧 worker 晚到的 saver 写入只能形成不可达 orphan，不得改写 accepted pointer。实际 claim、过期接管、重试/超时策略、heartbeat、执行边界 fencing 和 orphan 保留协议须在实现前完成决策，并以双 worker 暂停/复活与故障注入验证。

## Job 租约协议（2026-10-22）

内部 JobLeaseService 固定配置一个 tenant、独立 `job_worker` service executor 和进程实例标识；不新增 HTTP 领取或任意执行主体接口。每次操作按 Job 保存的 issuer/external subject 重新解析目录，核对 tenant/internal subject，使用当前 actor 属性与固定 worker executor 构造 Context。原 API executor、bearer 和提交时角色均不继承。`job:execute` 要求资源所有者、operator/campaign_admin，以及受信 job_worker executor；另检查场景已有业务权限。每个异步边界和租约事务提交前重新核验目录与 Policy，事务中拒绝先回滚再记录拒绝审计。

worker service ID 必须与该 requester 当前绑定的 API executor ID 不同；即使策略同时信任两种角色，也不能合并这两个执行身份。实例标识只属于一次进程生命周期，默认使用新 UUID，不能在不同进程启动间复用常量。

只按显式 Job ID 领取。queued 可领取；running 仅在旧 lease 已到期且没有取消请求时可重新领取。每次 claim 以完整快照 CAS 原子递增 epoch，并写同一事务的序号事件；首次执行 attempt=1，过期 running 的失败接管才递增 attempt，正常审批/事件等待之后的 queued 续行保持 attempt。否则招商的多次正常 HITL 会耗尽重试预算。该定义细化前期仅覆盖首次 queued→running 的计数约束；后续显式 retry 调度必须在发放新尝试时单独扣预算，不得伪装为等待续行。过期接管超过尝试预算或任何领取超过 Job timeout 时拒绝。lease_owner 绑定 service subject 和进程实例。服务端 UTC 时钟决定期限，租约最长不超过 Job timeout；本地支持确定性时钟测试，不将此证明外推为 PostgreSQL 分布式时钟验证。

heartbeat、checkpoint 指针和执行状态提交必须匹配 tenant/job/owner/epoch，且当前 lease 与 Job deadline 均有效。续租不增加 attempt/epoch，revision 单调递增；取消请求保留同一 lease，worker 只能确认 cancelled/failed，不能再提交成功或等待。持有旧 token 的操作不得推进已重新领取的 Job。queued 的等待/重试恢复、自动 timeout 回收与独立队列轮询仍须后续单独接入。

每个 epoch 的 checkpoint namespace 固定为服务端派生值。只有经 tenant-scoped 官方 saver 确认存在于该 namespace 的 checkpoint ID 才能发布为 accepted pointer；发布仍要在 Job CAS 事务中检查租约、目录和 Policy。重新 claim 保留上一 accepted pointer，旧 epoch 的晚写仅是不可达数据。指针发布不删除 saver 数据，不自动清理 orphan。

同 epoch 的 accepted pointer 只能沿当前已接受 checkpoint 的父链推进；祖先、兄弟和不完整父链拒绝，最多回溯 256 个 checkpoint，避免无限读取。相同指针的重试仅检查当前身份、租约与 saver，不新增状态事件。等待态必须持有本 epoch 的 accepted pointer，不能直接复用上一 epoch 指针冒充当前进度。

租约原语本身只保护 Job 行和 accepted pointer。以下两节仅补充显式本地草案提交与受审批约束的券 reservation 边界，其余领域库提交、其他外部 reservation 及实际外部调用前 fencing 尚未接入，不提供 Graph 执行入口；只有必需边界全部接通并验收后才能声明实际 Job/worker 接管安全。

## SQLite 本地草案执行边界（2026-10-22）

显式装配的内部 `JobExecutionService.persist_campaign_draft(token, spec, snapshot)` 只处理 campaign Job 的本地草案；没有默认 HTTP/Tool/Graph 入口，不接受任意 Context、回调、SQL、工具名或外部 adapter。复用原 JobLeaseService 的 requester 重载、独立 worker、owner/场景 Policy 与 token 验证，并把业务 Policy 资源绑定到实际 campaign ID。campaign/coupon/recruitment ID 沿用 Job 输入适配的派生规则，报名 mode 必须匹配 Job；规则快照与物料/补偿版本仍由受信服务端调用方提供，不代替规则检索或完整 Workflow 业务校验。

仅支持显式 SQLite 双库，JobRepository 必须属于所提供的 platform session factory。平台事务用完整快照条件的 no-op UPDATE 获得 SQLite 数据库写锁，重查 running/未取消和有效 lease，保持到业务事务提交或回滚结束，随后回滚 no-op 释放锁。不以仅提交前 SELECT 或进程内锁冒充 fencing。其他连接的 claim/heartbeat/cancel 写入不能越过该锁；取消排在业务 COMMIT 后时不能撤销已经提交的草案。锁会阻塞其他平台写入，边界内没有模型、网络或审批等待，仍限定 Community 单 worker。

一个业务事务保存四草案实体、固定 `persist_campaign_draft` ledger reservation→local success、Domain/Audit Event 和 outbox。复用现有 Repository 的 in-session 写入；通用 ExecutionLedger 与非 Job domain 路径不自动获得 fencing。按 tenant/job/操作派生稳定 reservation key，规范参数 hash 覆盖 spec 与 snapshot ID/hash；重复调用仍重查当前身份/Policy/lease，确认持久草案未变化后返回，同一 key 换内容拒绝。草案逻辑时间采用 Job.created_at，实际 ledger/event 时间采用操作时钟。ledger 的 `checkpoint_id` 为 `job:<job_id>:epoch:<epoch>:campaign-draft` 逻辑执行标记，只保留首次提交 epoch，**不是官方 saver checkpoint 或 accepted pointer**，不能用于 Graph resume。

事务内 await 后及业务提交入口均重新检查目录、Policy 和期限。LocalPolicy 的 async evaluation 与同步 `evaluate_now` 共用同一决策实现；SQLAlchemy before_commit 同步复核不打开平台审计 writer。Policy 拒绝在业务回滚并释放平台锁之后，经原审计入口记录。身份目录变化失败关闭，不能以 lease token 或旧角色继续写。

**原子性限于业务库内部，不提供跨库事务。** 平台锁只串行化 Job 写入，业务成功而平台锁释放/响应失败时，已提交的业务事实仍存在，重试须按 ledger/稳定 ID 读取。最终授权/期限检查在 COMMIT 入口，无法追溯撤销数据库驱动已接收 COMMIT 后的目录/Policy 撤权、时钟跨越或任务取消；目录/Policy 与业务库没有共同事务。未来若要求更强的撤权线性化，需要另外的受信版本/事务契约，不能靠重读宣称已保证。

真实临时 SQLite 的两独立 engine/repository 实例验证接管/取消 UPDATE 被锁阻塞，旧执行到期后整组回滚、新 epoch 可写，以及取消在提交后生效。竞争用例在持锁前完成真实授权，保留事务内当前身份/Policy guard，使测试直接到达 CAS UPDATE，而不是仅阻塞在授权审计。该证据不包含独立 worker 进程或 PostgreSQL；S2 和 T03/T04/Core 继续未完成。无 migration、依赖或环境变量。实施前设计、严格 RED→GREEN 和限制见[执行 fencing 验证卡](../../reports/verification/v0.6/20261022-t03-execution-fencing/summary.md)。

## SQLite 受审批约束的券 reservation（2026-10-22）

显式内部 `JobExecutionService.reserve_coupon_materialization(token, args, plan, approval_id)` 仅接受固定 `MaterializeCouponBatchArgs`/`LaunchPlan`，不接受 checkpoint、Context、SQL、callback、自由 tool 或 adapter；没有 HTTP/Tool/Graph 默认入口。复用草案切片的 SQLite 装配检查、当前 requester/独立 worker/owner/租约，以及实际 campaign 资源上的 draft 和 launch Policy。旧非 Job `domain.materialize_coupon_batch` 仍有 reserve-first/dispatch 语义，未被此方法调用，也不因此自动 fenced。

平台完整快照 no-op UPDATE 持锁到业务事务结束，并把同一平台事务交给既有 session-bound ApprovalRepository 只读加载真实审批。平台写锁阻止审批撤销、Job cancel/接管穿过该区间；不能在锁内调用会另开审计 writer 的普通 authorize/authorize_resume。ApprovalService 的纯 binding/expiry/status 判定抽为同一函数，非 Job 的 invalidation/expiry 审计流程仍保留。Job 路径初始审批失败在 ledger reserve 之前拒绝，并额外绑定 requester/SOD；launch 不接受 downstream business binding。Policy 拒绝按原 deferred audit 协议在回滚、解锁后记录。

业务事务先用 campaign no-op UPDATE 取得 SQLite 写锁，再读取四个持久草案和成功的 Job draft ledger receipt；仅允许原始 version=1、未 launch 的草案。用现有 CampaignDraft/hash 与 build_launch_plan 复核完整当前计划、规则/物料/补偿版本、固定两个 child 的 canonical args/hash/scope。campaign/coupon/recruitment 均为 Job 派生 ID。该业务写锁保证其他业务 writer 不能在读取后改动草案或 saga。checkpoint **只来自受信仓储已持久化且 planned 的 saga**，与真实 approval 的 checkpoint 严格匹配；这是既有 saga 逻辑审批点，**不是 Job accepted saver pointer**，不用于 Graph 恢复、不推进 accepted pointer。测试的前置审批经 ApprovalService 创建/独立主体决定，saga 经既有 Repository 准备；未连真实 Job Graph。

授权通过后只调用既有 tool_executions 的 in-session reserve，复用 canonical_args_hash 与 ExecutionLedger 的 `campaign_id:coupon_spec_hash` 稳定业务 scope/key，不另建账本。固定 Job 操作 execution ID 防止同 Job 另写不同内容；与既有账本同 key 的历史可以互通，但必须仍为 reserved 且 checkpoint 受当前审批覆盖。重复和跨 epoch 均重新鉴权/验租约/验事实，旧 token 即使存在历史也拒绝。reservation 不授予任何外部调用权限，不标 executing/succeeded，不改变券、saga、Job 或 accepted pointer。

await 后及业务 COMMIT 入口以当前目录和只读 Policy 同步复核 requester、worker、owner、实际资源、审批 policy_version/expiry 和 lease/deadline。approval 行在平台锁内稳定、业务事实在业务锁内稳定；外部 writer 排在业务提交后完成时，不能追溯撤销本次已提交 reservation。**原子性仅限业务库单事务，不是跨库 2PC**；COMMIT 交给驱动后的目录/Policy 撤权、时间跨越或 cancellation 不保证追溯回滚。业务提交后平台释放/响应失败仍应按稳定账本读取。

仅合成临时 SQLite，两资源实例竞争须到达实际 Job/approval UPDATE，另验证业务事实 UPDATE 被阻塞；不是 PostgreSQL/S2 或独立 worker 证明。无新依赖/migration/环境变量。Graph、dispatch fencing、其他领域、调度/恢复与 Postgres S2 未实现，T02/T03/T04/Core 未关闭，V0.5-T07 29/80 不变。实施前审查、RED/GREEN 和实测结果见[本轮验证卡](../../reports/verification/v0.6/20261022-t03-coupon-reservation/summary.md)。

## 实施前基线

ADR-014 已为状态、预算、lease 与 accepted pointer 建列，并在受信 Repository 的 queued→running 转换要求 attempt/epoch 各递增一次。当前没有 claim、heartbeat、worker runner 或过期回收；通用 CAS 不能证明有效 lease，也不能保护领域写入或外部副作用。测试内构造 running/waiting 状态只验证取消及拒绝陈旧快照，不代表已经执行或恢复 Graph。

上述基线是本轮实施前的事实。SQLite 并发/CAS 验证不能作为多 PostgreSQL worker 或完整副作用 fencing 的证明；T03/T04 门禁继续保留。

本地实现与未接入边界见[租约验证卡](../../reports/verification/v0.6/20261022-t03-job-leases/summary.md)。
