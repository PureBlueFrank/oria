# ADR-025：Job lease 与 fencing 恢复

- 状态：已接受（身份恢复、Job 租约、显式重试/到期处理及 SQLite 草案/launch 审批/reservation/受控 Mock 派发切片）；真实外部副作用、其余领域、runner 和接管验收仍待实施
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

## SQLite Job launch 审批准备（2026-10-22）

显式内部 `JobExecutionService.request_launch_approval(token, args, plan)` 补齐持久 Job 草案到真实 launch 审批的前置流程。固定 typed 参数与完整 LaunchPlan；审批 ID、planned saga、逻辑 checkpoint 均从 Job 派生，调用方不能传入 checkpoint、expiry、Context 或 adapter。除当前 requester/独立 worker/owner、实际 campaign draft/launch 权限外，还检查既有 `approval:launch:request`。新审批期限为服务端申请时起最多一小时，且不超过 Job timeout。

两阶段之间允许显式恢复，不提供跨库原子性。第一阶段持平台完整快照写锁，在业务单事务中验证原始完整草案、成功 draft receipt 与计划，幂等创建固定 planned saga。第二阶段重新获取平台锁和业务写锁，再次核对全部事实及固定 saga，通过同一平台事务创建 pending approval、approval.created 审计和既有状态 outbox。第二阶段业务事务只持 no-op 写锁，保持至平台提交结束后回滚；平台提交后不再写业务事实。平台锁帮助器允许调用方在该阶段显式提交审批事务，finally 的 rollback 只释放尚未提交的事务，不追溯撤销已提交审批。

阶段间失败可留下无审批的 planned saga；它不能授权券 reservation 或外部执行。有效 Job 重试补齐同一审批。平台提交成功而响应丢失时，重试返回相同审批/期限而不重复审计或延长有效期。每次历史读取和跨 epoch 重试仍重新检查身份、Policy、租约、当前完整草案及 saga。冲突、拒绝、失效、过期或 policy version 已变化的审批失败关闭，不自动生成另一张审批；重新申请/新版本流程尚未接线。

await 后及两种 COMMIT 入口均复核当前目录/Policy/lease；平台提交还复核审批绑定与有效期。取消、接管及业务 writer 排在锁后执行，不撤销已经提交的审批；驱动接收 COMMIT 后的撤权/时钟变化/cancellation 仍受前述限制。审批决定复用现有 ApprovalService，要求独立 decider；本入口不决定审批、不预留券、不调用 adapter、不改变 Job 状态或 accepted pointer，逻辑审批点不是官方 saver checkpoint。

仅临时 SQLite/合成身份，未接入默认 API/Graph/worker；不证明 PostgreSQL、多进程 worker 或 S2。实现与验证见[本轮验证卡](../../reports/verification/v0.6/20261022-t03-launch-approval/summary.md)。外部 dispatch fencing、其他领域写入、Graph/异步恢复、T02 真实交互缺口及 T03/T04/Core 门禁保留，V0.5-T07 仍为 29/80。

## SQLite Job 券的受控 Mock 派发（2026-10-22）

显式内部 `JobCouponDispatchService.materialize(token, args, plan, approval_id)` 只接受既有 `InMemoryCouponBatchAdapter` 的精确类型和固定 coupon 参数；子类/网络 Adapter 被拒绝，超时上限为 5 秒。没有默认 API、Tool、Graph、worker 装配或真实外部连接。现有 `CouponBatchAdapter` 没有消费 Job epoch 的 fencing 参数，因此此实现**仅验证 Community Mock 状态闭环，不宣称真实外部派发已安全**。后续真实 Adapter 必须先明确调用接纳 fencing、稳定幂等键及结果未知后的对账协议，不能只在网络请求前检查一次 lease。

派发前仍重新加载 requester、独立 worker、owner/epoch、Job 状态、实际 campaign launch Policy、批准的完整 LaunchPlan 和稳定 coupon reservation。平台 SQLite 写锁覆盖固定 Mock 调用窗口；业务库先原子提交 `reserved → executing` 和 admitted 事件，再执行 adapter。Mock accepted 后，在一个业务事务内提交 coupon `draft → materializing → ready`、saga `planned → coupon_materialized`、ledger succeeded、Domain/Audit/outbox；rejected 记为 failed，unknown/异常/超时记为 unknown。exception 内容不持久化。

若进程取消、结果提交失败、lease/Policy 失效，先前已提交的 executing 行不会回滚成 reserved；有效重试把遗留 executing 收敛到 unknown，unknown/failed/succeeded 均不再调用 adapter。成功重试只复用原结果并再次验证 ready coupon、原始审批计划和 saga。审批/Job/Business SQLite 写锁会阻塞其他连接直至 Mock 结果提交或回滚；这是 Community 单 worker 证据，不代表 PostgreSQL/S2、真实网络、独立 worker 或跨库 2PC。COMMIT 交给驱动后撤权/时钟变化不保证追溯撤销。

验证限临时 SQLite、合成身份与 Mock；覆盖成功与重复、拒绝/未知、异常/超时、取消、结果 COMMIT 失败、调用期间撤权/租约过期、新旧 epoch，以及两个独立 SQLite 资源实例的写锁。实现前决策、原始失败、定向结果、全库门禁和限制见[派发验证卡](../../reports/verification/v0.6/20261022-t03-coupon-dispatch/summary.md)。真实外部 adapter、对账/补偿、其他领域 fencing、Graph/调度/恢复及 PostgreSQL S2 仍未完成。

## 显式重试与到期处理（2026-10-22）

`JobLeaseService.schedule_retry(token, delay_seconds=...)` 记录受信执行器的重试决定。仅接受当前有效 running lease、未取消且有剩余尝试预算的 Job；延迟为 1–86400 秒的整数，retry_at 必须严格早于绝对 timeout。提交后为 retry_scheduled，清除 lease，保持 attempt/epoch 和 accepted pointer。它不分类错误、不调用模型/Adapter，也不授权重复未知副作用。

`release_retry(job_id)` 仅将到期但未超时的 retry_scheduled 变成 queued，同一完整快照 CAS 内预占下一 attempt 并记录事件；随后 claim 保持该 attempt，只增加 epoch。重复或竞争 release 至多一个成功。普通 Repository 转换不能将 retry_scheduled 伪装成不计费的等待续行。正常审批/事件等待的 queued 续行仍不扣失败预算；该接口不能恢复 waiting_approval/waiting_event。

`expire(job_id)` 仅将达到绝对 timeout 的非终态 Job 关闭为 failed，或在已请求取消的 lease 到期后关闭为 cancelled；两者同时满足时取消优先。仍有效的执行租约不被抢占，accepted pointer 不改变，终态重复调用不新增事件。普通 running lease 过期但 Job 未超时仍由既有 claim 接管；没有自动轮询、重试分类或 worker/reaper。

三个操作都重载当前 requester、要求独立受信 worker 与当前 Policy。每次 await 前后及同步 COMMIT 入口重查身份/权限/时间，状态与事件同一事务 CAS；失败整笔回滚。显式 expire 也不绕过撤权，因此被撤权的 Job 可能继续保留原状态，需未来有明确权限的管理流程处理。COMMIT 已交给驱动后的外部撤权或时钟变化不保证追溯撤销，与前述边界一致。没有新增 migration、依赖或环境变量。

本地临时 SQLite 回归覆盖预算、竞争、取消、期限、事件失败、身份/Policy 撤销及最后提交入口变化，见[调度验证卡](../../reports/verification/v0.6/20261022-t03-job-scheduling/summary.md)。外部接纳契约另见 [ADR-040](ADR-040-fenced-dispatch-admission.md)；两项均不代表 Graph、自动恢复或真实外部派发已接通，T03/T04/Core 继续进行中。

## 提交入口复核与有界调度（2026-10-23）

main 审查发现 `replace_leased` 在最后一次 async guard 返回后到实际 COMMIT 入口之间缺少同步复核；身份撤销虽可能在服务返回时被发现，Job 行与事件已经提交。claim、heartbeat、checkpoint 与 finish 现与显式调度一致：Repository 要求同步 commit guard，并在其前后复核当前时钟与 lease。12 项提交入口故障注入先失败后通过。边界仍是 COMMIT 交给驱动之前，不承诺外部目录与数据库跨资源原子性。

新增内部 `JobScheduler(leases, batch_size=100, interval_seconds=1)`，由显式调用 `run_once()` 或拥有停止事件的宿主 `run(stop)` 启动。单次最多读取 100 个同租户非终态 Job 引用，按 job_id keyset 分页，完成一轮后回绕；每个任务重读当前行，由既有 `release_retry/expire` 执行当前授权与完整快照 CAS。权限拒绝、数据不可用、竞争分别计数，不保存业务内容或异常文本；坏记录或撤权记录不能使后续页永久饥饿。扫描失败按固定间隔等待，取消传播并释放事务资源。

只释放已记录的到期 retry、关闭绝对超时和收尾取消，不领取执行租约、不推断错误可重试性、不恢复审批/事件等待、不触发 Graph/模型/Adapter。数据库状态是恢复依据，游标和统计可丢弃；重启从头扫描仍由 CAS 防重复。进程内锁只保护扫描游标，不作为多 worker 安全依据。尚无默认 API 装配、`worker run` 或特权撤权清理；SQLite 本地证据不替代 PostgreSQL S2。见[本轮验证卡](../../reports/verification/v0.6/20261023-main-review-scheduler/summary.md)。

## 实施前基线（历史）

ADR-014 已为状态、预算、lease 与 accepted pointer 建列，并在受信 Repository 的 queued→running 转换要求 attempt/epoch 各递增一次。当前没有 claim、heartbeat、worker runner 或过期回收；通用 CAS 不能证明有效 lease，也不能保护领域写入或外部副作用。测试内构造 running/waiting 状态只验证取消及拒绝陈旧快照，不代表已经执行或恢复 Graph。

上述基线是本轮实施前的事实。SQLite 并发/CAS 验证不能作为多 PostgreSQL worker 或完整副作用 fencing 的证明；T03/T04 门禁继续保留。

本地实现与未接入边界见[租约验证卡](../../reports/verification/v0.6/20261022-t03-job-leases/summary.md)。
