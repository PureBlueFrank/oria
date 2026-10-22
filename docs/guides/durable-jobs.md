# Durable Job 本地受理（V0.6-T03 进行中）

已支持创建会话、幂等提交两类结构化任务、查询和取消，状态与事件持久保存到 SQLite。两场景的 Graph 输入适配也已有独立本地验证。当前服务只受理任务，`queued` 不会自动开始执行；实际调度、飞书槽位对话、事件恢复与独立 worker 仍待接入。

## 显式启用

沿用 [API 身份配置](api-foundation.md)，在服务端 JSON 中增加：

```json
{
  "jwt": {
    "issuer": "https://identity.example",
    "audience": "oria",
    "jwks_url": "https://identity.example/.well-known/jwks.json"
  },
  "identity_file": "identities.json",
  "approval_runtime_config": "runtime.yaml",
  "job_service": true
}
```

`job_service` 默认为 false，只接受布尔值。它要求显式运行时文件，目前限定 Mock/Fixture 与 SQLite，共用审批运行时；`--local` 不自动初始化或启用存储。运行时 YAML 采用原有格式，例如 `data_dir: /absolute/path/to/initialized-test-data`。部署方先通过既有 `oria db upgrade` 入口显式升级；API 只读验证 `platform_0013`/`business_0011` 及 schema，缺库或旧库会停止启动，不自动迁移用户数据。

```bash
uv run oria api serve --config /path/to/private/server.json
```

启用后 `GET /v1/health` 中 `job_service` 为 `admission_only`。未启用时已认证的任务请求返回 503；未认证请求仍按原鉴权规则拒绝。没有新增环境变量。

## 请求

所有请求均使用既有 JWT。以下示例是 JSON 请求体，不含可用身份或令牌。

1. `POST /v1/sessions`，请求 `{}`，返回 201 及服务端生成的 `session_id/created_at`。
2. `POST /v1/jobs`，必须携带 `Idempotency-Key: example-20261022-1`，返回 202 和 Job 当前状态。用第一步的 session ID 替换示例值。

归因：

```json
{
  "session_id": "session_0123456789abcdef0123456789abcdef",
  "spec": {
    "scenario": "attribution",
    "metric": "redemptions",
    "period": {"start_date": "2026-07-01", "end_date": "2026-07-07"},
    "region": "上海"
  }
}
```

招商：

```json
{
  "session_id": "session_0123456789abcdef0123456789abcdef",
  "spec": {
    "scenario": "campaign",
    "categories": ["餐饮"],
    "cities": ["上海"],
    "enrollment_mode": "hybrid",
    "max_candidates": 10
  }
}
```

归因沿用 AnalyticsPeriod（起止日期之差最多 366 天，含首尾最多 367 个日历日）与枚举指标，可选 region/category。招商每组过滤值最多 10 个且不能重复，候选上限 1–100，报名模式沿用 `merchant/auto/hybrid`。HTTP 不接受自由自然语言或客户端自报 tenant、actor、role、checkpoint、wait、lease 和 status。未知字段返回 422，错误不回显输入。

3. `GET /v1/jobs/{job_id}` 查询自己的任务。只返回任务/会话 ID、场景、状态、revision、尝试预算、取消标记和时间；不返回原始提交、主体引用、内部 thread/run/checkpoint、lease 或幂等键。
4. `POST /v1/jobs/{job_id}/cancel`，可不传请求体或传 `{}`。queued/waiting/retry 直接取消；running 只请求取消，重复请求返回已有状态。成功/失败终态返回 409。当前 API 不会自行创建 running/waiting 状态。

## 幂等、授权和恢复

幂等键仅允许 1–128 个 ASCII 字母、数字或 `._:-`，数据库只保存摘要。范围为可信租户与内部主体；同键同内容重试返回已有 Job 的当前状态，同键换内容或换 session 返回 409。创建新任务应换键。客户端应保存 session/job ID；服务重启后仍可凭原 JWT 查询或用原键重试。

operator 与 campaign_admin 可创建自己的会话并提交、读取和取消自己的任务；招商另需既有 `campaign:draft:write`，归因另需 `analytics:read`。不能读取、提交到或取消其他人的会话/Job；不存在、跨主体和跨租户统一 404。每次操作及提交前重查身份和 Policy，撤权期间的写入会回滚。排队授权不能替代未来 worker 的业务授权。

状态与序号事件同事务保存，事件写失败时 Job 及幂等占位也回滚。取消是协作式请求，不撤销已提交的数据库或外部副作用。新迁移不会覆盖审批数据；只要新表有任一记录就拒绝降级，不自动删除历史数据。

本增量仅验证临时 SQLite、Mock/Fixture、JWT/Policy 与独立进程持久读取/应用重启；没有运行新 PostgreSQL、真实模型或真实飞书。飞书文本回调仍缺少 Job 消息处理器，不因打开本开关而被受理。见 [ADR-014](../adr/ADR-014-durable-job-admission.md)、[ADR-025](../adr/ADR-025-job-lease-and-fencing.md)和[受理验证卡](../../reports/verification/v0.6/20261022-t03-job-admission/summary.md)。

## 两场景 Graph 输入适配（2026-10-22）

受信内部调用可使用 `oria.orchestrator.job_inputs` 的 `campaign_job_input(job, ctx, template=...)` 与 `attribution_job_input(job, ctx)`。两者只生成现有 Graph 的初始 state，不调用模型、写数据库或推进 Job。Job 必须为全新 queued、attempt/epoch 为 0、没有取消请求或 accepted checkpoint；Context 中的 tenant、owner、session/thread/run/job ID 必须逐项一致，executor 必须为同租户 service，恢复 Context 不能用来重建初始输入。也会重新验证持久快照与 request hash。

招商必须显式传入服务端 `ScenarioAWorkflowRequest` 模板，沿用已有物料/补偿、选品、投放、通知和有效期契约；不调用 CLI 的固定合成默认值。适配层只从已确认槽位生成规范请求，设置候选上限/报名模式，以 Job ID 派生稳定业务 ID 和幂等键，effective_at 取提交时刻。两个等待有效期必须晚于该时刻且不超过 Job timeout，超出直接拒绝，不静默截断。投放和通知策略继续由服务端模板决定。

归因复用既有 `initial_attribution_state`、ResearchSpec 和固定版本 Prompt，将指标、起止日期、区域/类目编码作为规范数据输入，并绑定当前租户。执行方仍须显式装配已有的只读 AnalyticsQueryStore 和五工具 registry，沿用归因预算、证据校验、动态选路和弃答规则。输入适配不提供业务数据，也不生成或猜测结论。

本地输入契约 E2E-F 已覆盖招商到达真实审批中断及归因的两种不同调查路径；这不代表持久化 Job 已执行、已恢复或已获得执行权。HTTP 仍为 `admission_only`。身份恢复与 Job 租约原语见下节；实际调度和完整领域/外部副作用 fencing 尚未接入；仅下节后新增的显式本地草案边界受保护。详见[输入适配验证卡](../../reports/verification/v0.6/20261022-t03-graph-inputs/summary.md)。

## 内部身份恢复与 Job 租约（2026-10-22）

`JobLeaseService` 是受信内部接口，未装配到默认 API，也没有队列轮询或 Graph runner。配置必须显式提供已被 LocalPolicy 信任的独立 service executor，角色含 `job_worker`；API executor 的 `runtime` 角色不能替代它。每个实例具有独立 owner，服务固定到该 executor 的 tenant。服务不会自动把传入的 principal 加入可信集合。

worker 与当前目录中的 API executor 必须使用不同的 service ID，双角色或额外策略信任不能替代这条隔离。实例标识默认为每次新建的 UUID；显式 instance_id 仅用于当前进程生命周期，不能在不同启动间复用固定值。

每次操作从 Job 保存的 issuer/external subject 重载当前 actor，核对原 tenant/internal subject；只继承业务发起人的稳定身份，不继承其原 API executor、bearer 或角色快照。`job:execute` 同时要求所有者、operator/campaign_admin 和受信 worker，招商另查 `campaign:draft:write`，归因另查 `analytics:read`。读取后、异步鉴权后及事务提交前均重查目录与 Policy。当前 LocalPolicy 的可信 principal 集合由启动装配；目录属性变化会失败关闭，新的合法属性仍需受信运行时/策略重新装配。

`claim(job_id)` 以完整快照 CAS 领取 queued 或已到期的 running Job，同事务记录事件。每次领取都增加 epoch；首次执行 attempt=1，正常等待后的 queued 续行保持 attempt，过期 running 接管才增加失败尝试。超过失败预算、取消状态或 Job timeout 均不能接管。租约默认 30 秒，可在内部配置为 1–300 秒，并受 Job deadline 限制。`lease_token(claimed_job)` 只提取已保存的 tenant/job/owner/epoch；单凭 token 不能取得任何权限。

`heartbeat(token)` 续租且保持 attempt/epoch；`finish(token, status)` 只允许合法终态/等待态并清除 lease。取消请求后只接受 cancelled/failed，不能再提交成功或等待。每次写入前后均检查租约期限，旧 token、不同实例和撤权会拒绝；写入或事件失败同事务回滚。此处的 finish 是受信执行器原语，不能据此把未运行的业务标为成功；未来 runner 仍须验证 Graph 结果。

`publish_checkpoint(token, checkpoint_id)` 要求显式 tenant-scoped 官方 saver。namespace 只能由当前 Job/epoch 派生，不能由调用方指定；checkpoint 必须已存在。同 epoch 仅能沿 accepted 父链推进（最多回溯 256 个），重复发布同一指针不新增事件，祖先/兄弟/其他 namespace 均拒绝。指针推进仍重新鉴权并走租约 CAS；新 epoch 保留原 accepted pointer，旧 epoch 的迟到写入不改变它。orphan 清理尚未实现。

本地已覆盖两执行实例与两独立仓储进程竞争、续租、暂停过期、接管后旧 token、取消、真实 SQLite saver 指针与 lineage、目录/Policy 撤权及回滚。仓储子进程用受信测试 guard 验证 SQL/CAS，不代表完整 worker 进程或生产接管。上述租约原语仅保护 Job 行和 accepted pointer。本地草案另有下节的显式 opt-in 边界；其余领域提交、外部副作用 reservation/调用前 fencing、自动 retry/timeout 回收及独立 worker 尚未接入；不得将其用于真实任务自动执行。没有新环境变量、迁移或外部连接。见 [ADR-025](../adr/ADR-025-job-lease-and-fencing.md)和[本轮验证卡](../../reports/verification/v0.6/20261022-t03-job-leases/summary.md)。

## 显式本地草案 fencing（2026-10-22）

受信 worker 可显式构造 `JobExecutionService(leases, databases)`，调用 `persist_campaign_draft(token, spec, snapshot)`。两项装配参数必须来自同一 platform session factory，且 platform/business 都为独立文件的 SQLite。没有新增配置开关、HTTP/Tool 入口或 Graph runner；默认受理路径不调用它。仅用于 campaign Job 的本地草案，不授予 launch、外部工具 reservation 或发送权限。

spec 的 campaign/coupon/recruitment ID 必须分别为 `campaign_<job suffix>`、`coupon_<job suffix>`、`recruitment_<job suffix>`，与输入适配一致；snapshot 必须为相同 tenant、hash 有效，报名 mode 匹配 Job。snapshot 及物料/补偿版本由受信服务端调用方准备，本接口不替代规则查询或审批。调用方不能传入 Context、任意执行回调或 SQL；token 仍需当前 requester 和独立 worker Policy 授权，业务权限检查使用实际 campaign ID。

本接口持平台库 SQLite 写锁直至业务事务结束，阻止其他连接的取消/接管穿过业务 COMMIT。四草案实体、固定本地操作的 ledger reservation/成功结果、Domain/Audit Event、outbox 在**一个业务事务**中提交，出错或提交入口撤权/到期则整组回滚。相同内容重试不新增事实，跨 epoch 可读回原结果，但仍检查当前身份、Policy 和 lease；换内容或已改变的草案拒绝。非 Job 的原草案服务和通用 ledger 行为保持原样，不自动获得 fencing。

平台锁不等于跨库原子事务；业务提交后响应失败，业务记录仍可能已经存在，须用同一 Job/参数重试读取。取消只能阻止后续提交，不能撤销已提交草案。最终身份/Policy/期限检查位于 COMMIT 入口；数据库驱动已接收 COMMIT 后发生的撤权、时钟跨越或任务取消，不能保证追溯回滚。ledger checkpoint_id 只保存首次 epoch 的逻辑执行标记，不是 saver checkpoint，也不推进 accepted pointer。

锁期间会阻塞平台写入，边界内仅做本地数据库操作。真实临时 SQLite 两独立资源实例的接管与取消竞争已有定向测试；不代表 PostgreSQL 多 worker、独立 worker 进程或完整恢复安全。其余 domain/外部调用、Graph、调度和异步恢复未接入；T02 真实拒绝/分页缺口、T03/T04/Core 与 V0.5-T07 29/80 状态不变。设计和测试证据见 [ADR-025](../adr/ADR-025-job-lease-and-fencing.md)及[执行 fencing 验证卡](../../reports/verification/v0.6/20261022-t03-execution-fencing/summary.md)。

## 内部券 reservation（2026-10-22）

只可显式装配 `JobExecutionService.reserve_coupon_materialization(token, args, plan, approval_id)`；固定类型是 `MaterializeCouponBatchArgs` 与 `LaunchPlan`，没有 checkpoint、自由工具名、Context、回调或 adapter 参数，也没有默认 HTTP/Tool/Graph 入口。需要当前有效 Job lease、当前 requester/独立 worker/owner 和实际 campaign 的 draft/launch Policy。只处理该 Job 已成功持久化、原始 version=1 且未 launch 的四草案，并按既有 canonical hash 重建完整计划核对审批与 child。

前置审批必须真实存于 Platform Repository，approved、未过期、policy_version 当前且 requester/SOD 有效；checkpoint 只从 Business Repository 已持久的 planned saga 读取并严格匹配审批。本切片不创建 launch 审批或 saga；验证使用受信合成 fixture 经既有 ApprovalService/Repository 准备，未接入真实 Job Graph。该 checkpoint 是 saga 逻辑审批点，不是 accepted saver pointer；本方法不推进 Job 指针。

SQLite 平台完整快照写锁持有到业务单事务结束；审批在同一平台事务只读检查，不在锁内打开普通授权审计 writer。业务先取得 campaign 写锁，再读草案/saga，授权有效后才在原 ExecutionLedger 的稳定 canonical 业务 key 上 reserve。await 后及 COMMIT 入口重查当前身份、Policy、审批版本/有效期和租约；无效初始审批不会调用 ledger reserve。重复/跨 epoch 重用同 key，旧 token 即使已有 history 仍拒绝，内容变化冲突。只写 reserved，不发外部请求、不标 executing/success，也不授予 dispatch 权限。

业务库单库原子；这不是跨库 2PC。审批撤销/取消/其他业务写若排在本次 COMMIT 后生效，不撤销已提交事实；目录/Policy 在驱动收到 COMMIT 后撤权也不能保证追溯回滚。锁会阻塞其他 SQLite writer，两资源实例实测不代表 PostgreSQL/S2 或独立 worker。Graph runner、派发前 fencing、其他领域、调度/恢复仍未实现；T02/T03/T04/Core 未关闭，V0.5-T07 29/80 不变。证据见[本轮验证卡](../../reports/verification/v0.6/20261022-t03-coupon-reservation/summary.md)与 [ADR-025](../adr/ADR-025-job-lease-and-fencing.md)。

## 从 Job 草案申请 launch 审批（2026-10-22）

显式内部调用 `request_launch_approval(token, args, plan)`，其中 args 为既有 `MaterializeCouponBatchArgs`，plan 为完整 `LaunchPlan`。入口从持久草案和成功草案账本重新验证内容，检查当前 Job/身份/租约以及实际 campaign 的 draft、launch 和既有审批申请权限，然后创建 planned saga 与真实 pending approval。调用方不能指定 checkpoint、审批 ID、expiry 或 Context；逻辑审批点由 Job 派生，不是 saver checkpoint。审批最多有效一小时且不超过 Job timeout。

Business saga 与 Platform 审批分阶段提交：先持久化 saga，再重新锁定并核验业务事实，提交审批、审计和 outbox。中断可能留下无审批的 planned saga；同一有效 Job 重试补齐审批。已提交审批的重试返回相同 ID/期限，不重复创建，也不延长有效期。冲突、拒绝、失效、过期或旧 Policy 版本的历史均拒绝；重新申请新版本尚未接线。两个阶段均重查当前权限和租约，审批提交期间持有业务写锁，防止草案被并发修改。

返回值复用 `LaunchApprovalBinding`。独立审批人仍通过现有 ApprovalService 决定，批准后才能调用上一节的券 reservation。该链路仅用临时 SQLite 与合成身份验证，尚未接入默认 API、Graph、飞书或独立 worker；不改变 Job 状态/accepted pointer，也不执行外部券派发。见 [ADR-025](../adr/ADR-025-job-lease-and-fencing.md)和[验证卡](../../reports/verification/v0.6/20261022-t03-launch-approval/summary.md)。

## Job 券的受控 Mock 派发

显式内部 `JobCouponDispatchService(leases, databases, adapter).materialize(token, args, plan, approval_id)` 只接受精确的 `InMemoryCouponBatchAdapter`，不接受子类或网络 adapter，Mock 超时最多 5 秒。没有默认 HTTP/Tool/Graph/worker 入口。它重新校验当前 requester/worker/lease、campaign launch Policy、真实已批准且未过期的完整计划以及此前提交的 Job reservation。

业务库先持久化 `reserved → executing` 和 admitted 事件，再调用无网络的 Mock。accepted 在单个业务事务中更新 coupon、launch saga、ledger、Domain/Audit/outbox；rejected 为 failed，unknown、异常、超时为 unknown。若调用后取消、撤权或结果提交失败，executing 历史保留；下一次有效调用将其收敛为 unknown，任何终态或 unknown 都不会盲目重投。异常文本不进入持久事件。

这个切片只证明 Community Mock 闭环。既有 `CouponBatchAdapter` 没有 Job epoch fencing 契约，真实网络派发仍关闭；真实 adapter 实现前还需确定接纳 fencing、稳定幂等与未知结果对账协议。平台和业务 SQLite 写锁覆盖短暂 Mock 调用，另有两个独立资源实例的 writer 阻塞测试；这会阻塞同库 writer，不证明 PostgreSQL、多 worker/S2 或跨库原子性。Job 状态及 accepted checkpoint pointer 不改变，真实外部派发、自动对账/补偿、Graph runner 和恢复仍未接入。见 [ADR-025](../adr/ADR-025-job-lease-and-fencing.md)及[派发验证卡](../../reports/verification/v0.6/20261022-t03-coupon-dispatch/summary.md)。
