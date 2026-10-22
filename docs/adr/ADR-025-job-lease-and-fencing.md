# ADR-025：Job lease 与 fencing 恢复

- 状态：已接受（身份恢复与 Job 租约协议）；领域副作用 fencing、runner 和接管验收仍待实施
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

此协议只保护 Job 行和 accepted pointer。领域库提交、execution ledger reservation 及实际外部调用前的 fencing 尚未接入，不提供 Graph 执行入口；只有这些边界全部接通并验收后才能声明实际 Job/worker 接管安全。

## 实施前基线

ADR-014 已为状态、预算、lease 与 accepted pointer 建列，并在受信 Repository 的 queued→running 转换要求 attempt/epoch 各递增一次。当前没有 claim、heartbeat、worker runner 或过期回收；通用 CAS 不能证明有效 lease，也不能保护领域写入或外部副作用。测试内构造 running/waiting 状态只验证取消及拒绝陈旧快照，不代表已经执行或恢复 Graph。

上述基线是本轮实施前的事实。SQLite 并发/CAS 验证不能作为多 PostgreSQL worker 或完整副作用 fencing 的证明；T03/T04 门禁继续保留。

本地实现与未接入边界见[租约验证卡](../../reports/verification/v0.6/20261022-t03-job-leases/summary.md)。
