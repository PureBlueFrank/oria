# V0.6-T03：受审批约束的 coupon reservation

2026-10-22，Asia/Shanghai。基线 `c8100754817ed715eadc7d78da4630abccca92cb`，master clean 起步，HEAD/分支未变化。[review.md](review.md) 在测试和源码修改前保存。仅交付一个内部 opt-in typed reservation，**T02/T03/T04/Core 未关闭，V0.5-T07 29/80 不变**。

## 本轮结果

新增 `JobExecutionService.reserve_coupon_materialization(token, args, plan, approval_id)`，固定 `MaterializeCouponBatchArgs`/`LaunchPlan`，不接受 checkpoint、Context、SQL、callback、自由 tool 名称或 adapter。没有 HTTP/Tool/Graph 默认装配。有效路径只返回并保留原 ExecutionLedger 中的 `reserved`，不调用任何 adapter/外部请求、不标 executing/success，不授予 dispatch 权限，不推进券、saga、Job 事件或 accepted pointer。

复用上轮装配检查、当前 requester/独立 worker/owner、实际 campaign draft Policy 和完整快照 SQLite 平台 no-op UPDATE 锁；另检查实际 campaign 的 launch Policy。锁内通过既有 session-bound ApprovalRepository 读取同一平台事务的真实 approval。小幅抽取 ApprovalService 的纯 binding/expiry/status 判定，原非 Job invalidation/expiry 审计与 downstream business binding 校验仍保留。Job 路径只读拒绝不匹配的审批，额外检查 requester/SOD，拒绝不属于 launch 的 downstream binding。初始无效审批测试观测 ledger reserve 未被调用，不能以“先 reserve 后回滚”冒充审批先行。

业务单事务先以 campaign no-op UPDATE 获得 SQLite 写锁，再读取四草案、成功 Job draft receipt 与 planned saga；仅接受已持久、原始 version=1 且未 launch 的草案。复用 CampaignDraft/hash、build_launch_plan 和 canonical child hash/scope 重建当前完整计划；campaign/coupon/recruitment ID 绑定 Job 派生值。授权有效后才调用既有 `tool_executions` in-session reserve，key 使用原 `ExecutionLedger.build_idempotency_key(campaign_id:coupon_spec_hash, canonical_args_hash)`，没有另建账本。固定 Job 操作 execution ID 防止另写不同内容；相同 canonical key 与旧通用 ledger 互通，历史必须仍为 reserved 且 checkpoint 匹配。重复、跨 epoch、已有历史仍重新核验当前身份/Policy/lease/审批/事实。

checkpoint **仅来自受信仓储中持久化的 planned saga**，与真实 approval checkpoint 严格匹配；它是既有 saga 逻辑审批点，**不是 Job accepted saver pointer**。前置审批由合成 fixture 经既有 ApprovalService 创建并由独立 approver 决定，saga 经既有 Repository 准备；没有实现 launch 审批创建接线、真实 Job Graph 或恢复路径。另以真实 tenant-scoped saver 合成 checkpoint 验证已有 accepted pointer 不被使用或推进，不将此用例称为 Graph 恢复。

await 后与业务 COMMIT 入口复核当前目录、requester/worker/owner、实际资源 Policy、approval policy_version/expiry、lease/deadline。平台行在平台锁内稳定，业务事实在业务写锁内稳定；锁内不调用会另开平台审计 writer 的普通 authorize/authorize_resume。Policy 拒绝沿原 deferred audit 在业务回滚、解锁后记录。

## 验证覆盖与限制

相关行为测试覆盖：有效路径 reserved/无 adapter；相同 canonical key 的旧账本互通及非 reserved 拒绝；重复/跨 epoch 同 key、旧 token 即使有历史仍拒绝；内容、tenant/job/owner/epoch、业务 ID、requester、plan/child/scope/物料与草案版本、approval action/tool/hash/version/checkpoint 绑定；缺失/pending/rejected/expired/invalidated/异租户审批与 SOD；当前目录/actor/worker/实际 campaign Policy 撤权；cancel、lease expiry/deadline；await/COMMIT 入口撤权/版本变化/审批过期回滚；业务异常与 task cancellation 回滚及随后成功写入/heartbeat 证明锁释放。

真实临时 SQLite **两个独立 DatabaseResources/engine/repository 实例**验证 takeover、cancel、approval invalidate 的 UPDATE 在平台锁处等待；竞争者提前完成真实授权并保留真实事务 guard，观测 `before_cursor_execute` 已到实际目标 UPDATE，避免只因前置授权审计而阻塞。过期旧执行整笔 rollback，新 epoch 才可 reserve；排在提交后的 cancel/revoke 不撤销已提交 reservation，随后重试拒绝。另验证其他业务实例实际 publication version UPDATE 在业务锁处等待，提交后版本变化会使重试失败关闭。它们不是独立 worker 进程、PostgreSQL 或 S2 证明。

**原子性只限 Business DB 单库事务，不是跨库 2PC。** 业务提交后平台释放/响应失败，已提交事实仍须按稳定账本读取，不能冒称跨库回滚。最终授权/时效检查在 COMMIT 入口；驱动收到 COMMIT 后的目录/Policy 撤权、时钟跨越或 task cancellation 不保证追溯撤销。该边界复用上轮协议，不声称外部调用已 fenced。SQLite 平台/业务写锁会阻塞其他 writer，仍限定 Community 单 worker。

Graph runner、dispatch fencing、其他领域、调度/异步恢复、独立 worker、orphan 清理、Postgres S2 均未实现。T02 真实带理由拒绝/分页缺口、T03/T04/Core 门禁及 V0.5-T07 29/80 均保留。非 Job `domain.materialize_coupon_batch` 保持原 reserve-first/dispatch 语义，未被新方法调用，不因本轮自动 fenced。

## Orchestrator 独立验证

在实现方交付后、源码备份与互斥窗口内独立执行；未采信其自报结论。三项门禁串行运行，源码 SHA-256 在运行前后一致（[independent-results.json](independent-results.json) 的 `source_fingerprints_unchanged.mismatches` 为空），且五个受测文件与实现方 [source-snapshot.json](source-snapshot.json) 逐字节相同。

| 检查 | 实际结果 | 证据 |
| --- | --- | --- |
| 完整 Community | **1340 passed / 9 deselected / 5 warnings**，458.52 秒，退出 0 | [independent-community.log](independent-community.log) |
| Security | **381 passed / 968 deselected / 1 warning**，181.32 秒，退出 0 | [independent-security.log](independent-security.log) |
| 全库静态 | 384 文件格式、Ruff clean、**192 源文件 mypy clean**，退出 0 | [independent-lint.log](independent-lint.log) |
| wheel/sdist + 安装包 | 16 条命令全部退出 0：构建、全新隔离 venv、按 uv.lock 安装锁定依赖、wheel 的 `reserve_coupon_materialization`/`JobCouponReservationWriter`/`approval_resume_rejection` 直接导入、CLI 0.1.0、8 个 wheel verifier、wheel strict type consumer | [independent-package-clean.log](independent-package-clean.log)、[逐命令结果](independent-package-clean-results.json) |

完整门禁相对 1268 基线新增 **72 项**，不与 184 项定向或 381 项安全相加。

### 反假通过：变异测试

不满足于“测试通过”，对两处关键校验做真实变异并跑**整个新测试文件**，确认测试会失败，然后逐字节还原：

| 变异 | 结果 | 证据 |
| --- | --- | --- |
| 删除 Job 路径的 requester/SOD 校验（只保留 `rejection is not None`） | **2 failed / 70 passed**，退出 1：`test_invalid_approval_never_adds_reservation[requester]`、`[downstream_binding]` | [mutation-no-requester-sod.log](mutation-no-requester-sod.log) |
| 删除历史 reservation 的 checkpoint 相等校验 | **1 failed / 71 passed**，退出 1：`test_existing_ledger_cannot_supply_an_unapproved_checkpoint` | [mutation-no-checkpoint-equality.log](mutation-no-checkpoint-equality.log) |

两次变异均被捕获，说明这两处门禁不是空断言。还原后确认 `src/oria/storage/job_execution.py` 与实现方验证快照一致（`cb2075abccd8e2b3…`），随后的干净门禁即为上表结果。

### 独立复审未完成

原计划的独立只读 Codex 复审（`codex-auto-review`）在给出结论前遇到账号级用量上限，**未产出复审意见**，因此本轮没有“独立复审通过”这一证据；不做替代性声明。上面三项门禁 + 变异测试是本轮实际独立证据。此前一轮草案切片的独立复审结论不能沿用。

## 严格纵向 TDD 与失败历史

以下是各轮当时源码的真实独立结果，不累加，也不以中间通过替代最终验证。

| 顺序 | 结果/退出码 | 日志与说明 |
| --- | --- | --- |
| 前置夹具失败 | 1 error / 1 | [01-valid-red.log](01-valid-red.log)：Context 缺少 session/thread/run；**不计为行为 RED** |
| 有效路径行为 RED | 1 failed / 1 | [02-valid-red.log](02-valid-red.log)：可调用 fail-closed scaffold 拒绝有效审批，PermissionError；不是 import/missing method 错误 |
| 最小实现初版失败 | 1 failed / 1 | [03-valid-green.log](03-valid-green.log)：漏传既有仓储 tenant 参数，修正调用 |
| 最小 GREEN | 1 passed / 0 | [04-valid-green.log](04-valid-green.log)：真实 approval/saga/draft 有效，仅 reserved |
| 历史 checkpoint 行为 RED | 1 failed、2 deselected / 1 | [05-history-checkpoint-red.log](05-history-checkpoint-red.log)：旧历史 checkpoint 不匹配仍返回，DID NOT RAISE |
| 历史约束 GREEN | 3 passed / 0 | [06-history-green.log](06-history-green.log)：加历史 checkpoint 核验，重复/跨 epoch 同 key |
| 绑定扩展初版 | 10 failed、24 passed / 1 | [07-bindings.log](07-bindings.log)：publication 独立 version 未被旧 hash 覆盖，真实 DID NOT RAISE，补原始版本约束；其余为夹具误用只更新状态的 replace 修改不可变元数据，及库已禁止自批。修正为独立受信审批记录与真实自批拒绝，不放宽验证 |
| 绑定与异步/提交门禁 | 57 passed / 0 | [08-bindings-boundaries.log](08-bindings-boundaries.log)：含 expiry、撤权、cancel/deadline、错误/cancellation 回滚和锁释放 |
| 实际 UPDATE 竞争 | 6 passed、57 deselected / 0 | [09-races.log](09-races.log)：takeover/cancel/revoke 以及锁前取消/撤销、自批拒绝 |
| 首轮相关回归 | 183 passed / 0 | [targeted-final.log](targeted-final.log)：其后补齐无历史撤权分支、业务 writer 竞争，并调整事务 context 写法解决 lint，须按最终轮结果交付 |

静态初版失败全部保留：[10-lint-initial.log](10-lint-initial.log) 格式/导入/行长退出 1，mypy 退出 0；[lint-final.log](lint-final.log) 仍有一处 SIM117（退出 1），mypy 退出 0。仅修改受影响文件的格式/导入和新 coupon 事务写法；没有机械改写其他工作流。

## 最终命令与结果

所有 uv 命令均使用 `UV_CACHE_DIR=.artifacts/uv-cache UV_OFFLINE=1 UV_NO_SYNC=1`，未安装或同步依赖。完整命令、耗时与逐命令退出码见 [commands.jsonl](commands.jsonl)。

| 检查 | 最终结果 | 证据 |
| --- | --- | --- |
| 12 个相关测试文件，排除 live/enterprise/performance | **184 passed / 1 warning**，pytest **119.77 秒**，退出 **0** | [targeted-final-rechecked.log](targeted-final-rechecked.log)、[命令/指纹](targeted-result.json) |
| Ruff format --check，4 源文件 + 新测试 | **5 files already formatted**，退出 **0** | [lint-final-corrected.log](lint-final-corrected.log) |
| Ruff check，同上 | **All checks passed**，退出 **0** | [lint-final-corrected.log](lint-final-corrected.log) |
| mypy，4 个受影响源文件 | **Success: no issues found in 4 source files**，退出 **0** | [lint-final-corrected.log](lint-final-corrected.log) |

唯一 warning 为既有 Starlette/httpx 弃用提示。最终受测源码指纹在进程运行期间未变化。上轮两个草案实现函数 AST、既有验证卡、pyproject/uv.lock 与 HEAD 保持原样，见 [baseline-checks.json](baseline-checks.json)。完整改动文件见 [changed-files.md](changed-files.md)，源码/文档指纹见 [source-snapshot.json](source-snapshot.json)，新增文档引用与差异检查见 [documentation-checks.json](documentation-checks.json)。本地详细路线为仓库既有 ignored 文件，已更新但未暂存。

本轮实现方未运行完整 Community、独立全量 Security 或打包；这些由 orchestrator 独立执行，结果见下节。本助手未运行用户库、凭证、真实飞书、Live/Enterprise/Performance、新 PostgreSQL，无新依赖/migration/环境变量。没有 git commit/checkout/push、远程写入或 CI 触发；本轮 GitHub Actions 状态为未运行，未以旧提交 CI 冒充本轮证据。全部新增测试只用合成 fixture 与临时 SQLite。
