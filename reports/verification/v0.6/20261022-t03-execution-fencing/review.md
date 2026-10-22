# T03 执行 fencing：实施前审查

日期：2026-10-22。基线 HEAD `7e804ede91eb2e38a450f2413fefa17b9e50d897`，开始时工作区 clean。本文件先于测试及代码修改保存。

## 已核对与无需重复的实现

已读仓库 AGENTS.md、上一轮 job-leases/summary.md、ADR-025、详细路线 697–738 行，以及 job_leases、JobRepository、DatabaseResources、ExecutionLedger、ledger repositories、CampaignLaunchService 和 campaign draft repository。架构详细文档位于根目录 `Oria架构设计.md`，核对其中事务/真相源与 lease 段落，并参考 ARCHITECTURE.md。

现有 JobLeaseService 已重载当前 requester，隔离 API/worker，检查 owner/场景 Policy，完整 CAS，验证真实 saver 的 epoch namespace/accepted ancestry；不重复实现。现有业务库与平台库有独立 engine/session。草案四实体单业务事务，但没有 Job fence。ExecutionLedger 的 reservation 自开业务事务，不能通过提交前读一次平台 Job 获得原子防接管能力。现有非 Job 草案刻意没有 ledger/outbox，必须保留。

## 最小切片

新增显式装配、无默认入口的受信 `JobExecutionService.persist_campaign_draft(token, spec, snapshot)`，只支持 SQLite 双库和 campaign Job；不接 Graph。复用现有身份/租约授权与草案校验/构造，业务 ID 必须与 Job 输入适配既有派生规则一致，mode 与 Job 绑定。调用者不能传任意 Context、租户、SQL、回调、外部 adapter 或自由工具名。token 本身不授权。

平台 JobRepository 内部用完整快照条件的 no-op UPDATE 获得真正 SQLite 写锁，重新验证 running/未取消、tenant/job/owner/epoch、期限。持锁直至业务 COMMIT 完成或回滚；不同 repository/engine 实例的接管、heartbeat 与取消只能在锁释放后写入，不能靠进程内锁。保守地不支持 PostgreSQL，后续须另设计/实测行锁与 S2。

业务库一个事务中执行固定 `persist_campaign_draft` ledger reservation、四草案实体、local success、Domain/Audit Event 和 outbox。reservation key 按 tenant/job/操作固定，canonical hash 覆盖 spec 与 snapshot ID/hash；同内容重试返回同草案/结果，换内容拒绝，无重复事件。rule ref ID 稳定派生，不跨 epoch 重建。ledger checkpoint_id 只记录本次 Job epoch 的逻辑执行标记，不伪造 accepted saver checkpoint，也不推进 accepted pointer。没有独立 reservation 或外部调用授权能力。

事务中只使用不写审计的 Policy evaluation；拒绝需先回滚两库锁，再经既有审计入口记录。最后 async 复核后，在业务 SQLAlchemy before_commit 同步钩子重查目录/Policy/期限，防止进入 commit 前的 await 撤权/到期。Policy 复用同一确定性决策实现，不以 token 绕过。

## 一致性与限制

- 原子性只覆盖业务库内的草案、ledger、Domain/Audit/outbox。平台 no-op 锁不提交 Job 状态/事件；业务成功而释放平台连接或响应失败时，必须凭稳定 ID/ledger 重试读取，不能假设两库一起回滚。
- 数据库锁保证业务 COMMIT 在后续 claim/cancel 写入之前完成；取消请求排在其后不能撤销已提交草案。锁期间租约可能到期，但最终提交入口若已到期则拒绝；不宣称 DB driver 已接收 COMMIT 后的时钟跨越或外部目录/Policy 撤权能追溯撤销提交。目录/Policy 与业务 DB 没有共同事务。
- 仅这个显式 typed 方法受保护。既有非 Job domain/ledger 路径不自动获得 fencing，不允许把它们直接用于未来 Job runner。外部调用/其 reservation、其他领域服务、调度/恢复/orphan 清理均未接入。
- SQLite 数据库写锁会阻塞其他平台写事务；边界内没有网络/模型/Graph/审批操作，仍只适合 Community 单 worker。SQLite 跨实例竞争证据不能称为 PostgreSQL 多 worker 证明。
- 无依赖/迁移/环境变量；仅临时合成测试库，无用户库、凭证、真实飞书、Live、远程写入、commit 或 push。

## 验收与严格纵向 TDD 顺序

1. 先写取消 Job 不能写草案/占 reservation 的回归，真实运行并保存 RED；实现最小 GREEN 后再加其余行为用例。
2. 有效路径 + 同内容重试/跨 epoch 重试；不同内容冲突；只有一组草案/ledger/事件/outbox，Job 无状态推进。
3. 过期、接管后的旧 epoch、错误 tenant/job/worker/scenario/业务 ID/mode、未信任 worker 均拒绝且无业务残留。
4. 在业务 await 和进入 commit 前撤销当前身份、actor/worker Policy 或使时钟过期，必须回滚全部业务事实，拒绝审计不能在持平台锁期间死锁。
5. 在 reservation、实体、事件/outbox 写入处故障和 task cancellation，验证无部分实体/reservation/Domain/Audit/outbox，并验证锁已释放。
6. 两个独立 DatabaseResources/JobRepository 实例、真实临时 SQLite：持锁时另一实例 takeover/cancel 不可越过；过期旧执行回滚后新 epoch 可接管并成功；另一顺序 takeover 先完成则旧执行拒绝。
7. 只运行本切片及受影响 lease/draft/ledger/input/Policy 定向测试与 lint。所有实际 RED/失败日志原样保留并记录退出码；不跑全套、Live/eval 或新 PostgreSQL。文档同步真实范围，T02 真实拒绝/分页、T03/T04/Core、V0.5-T07 29/80 保持未关闭。
