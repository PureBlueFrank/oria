# V0.6-T03：显式 SQLite 本地草案执行 fencing

日期：2026-10-22，Asia/Shanghai。基线 `7e804ede91eb2e38a450f2413fefa17b9e50d897`，开始工作区 clean。[实施前 review.md](review.md) 先于测试和源码编辑保存。只交付一个内部 opt-in 切片，**T03/T04/Core 继续进行中**，T02 真实带理由拒绝/分页缺口和 V0.5-T07 **29/80** 不变。

## 已实现范围

`JobExecutionService.persist_campaign_draft(token, spec, snapshot)` 只支持 campaign Job 与 SQLite 双库，未装配到 HTTP、Tool 或 Graph。调用方不能传任意 Context、SQL、自由工具名、执行回调或外部 adapter。复用原 JobLeaseService 的当前身份恢复、独立 worker、owner/场景 Policy 与 token 校验，业务 Policy 使用实际 campaign ID，三个业务 ID 沿用 Job 输入适配的稳定派生，报名 mode 与 Job 绑定。snapshot 和物料/补偿版本由受信服务端调用方准备，不代替规则检索或完整 Workflow 校验。

平台库以完整快照条件的 no-op UPDATE 获取真实 SQLite 写锁，验证 running/未取消、tenant/job/owner/epoch 与有效期，持锁直到业务事务完成，再回滚 no-op 释放。业务库单事务保存四草案实体、固定本地操作的 ledger reservation→success、Domain/Audit Event 与 outbox；未改变 Job 状态、事件或 accepted pointer。规范内容冲突拒绝；重复调用及新 epoch 重试均重查当前授权，确认原草案事实仍完整且未变化，不重复新增记录。

事务 await 后复核当前目录/Policy/lease，在 SQLAlchemy before_commit 同步检查提交入口。LocalPolicy async/sync 复用同一决策，事务内不另开平台审计 writer；Policy 拒绝在回滚和释放锁后记录。规则快照通过既有 internal_payload 重验，保留公开序列化故意隐藏的 tenant/私有 scope。非 Job 的原草案仍不写 ledger/outbox，通用 ExecutionLedger 行为不变；只复用低层 in-session Repository，避免开放通用回调。

## 一致性边界与未完成项

- **原子性仅限业务库。** 平台锁阻止 claim/cancel/heartbeat 的写入穿过业务 COMMIT，不构成两库分布式事务。业务提交后平台释放/响应失败，业务事实仍存在；独立故障用例确认重试读取已有结果，不虚称回滚。
- 最终身份/Policy/期限检查位于 COMMIT 入口。驱动已经接收 COMMIT 后的目录/Policy 撤权、时钟跨越或任务取消，不能保证追溯回滚；目录/Policy 与业务库没有共同事务。取消排在提交后生效，不能撤销已提交草案。
- 草案逻辑时间为 Job.created_at；ledger/event 为首次操作时钟。ledger checkpoint_id 为 `job:<job_id>:epoch:<epoch>:campaign-draft` 逻辑标记，保留首次提交 epoch；它不是官方 saver checkpoint，不可用作 accepted/resume 游标。
- 仅这个 typed 方法受保护。其他 domain 服务、通用 ledger reservation、外部调用前 fencing、Graph runner、调度、审批/inbox 恢复、独立 worker 进程和 orphan 清理均未接入。
- SQLite 锁会阻塞其他平台写入；边界内只有本地数据库操作，仍限定 Community 单 worker。两独立资源实例的真实临时 SQLite 竞争证明数据库锁/CAS 顺序，**不证明 PostgreSQL 多 worker 或 S2**。竞争测试提前完成真实授权，保留事务内真实 guard，让竞争到达实际 UPDATE，避免把授权审计阻塞误当作目标写入的锁证明。
- 无新依赖、migration、环境变量、默认开关或外部入口。只使用合成数据和临时测试库；没有用户数据库、凭证、真实飞书或远程写入。

## 严格纵向 TDD 与失败历史

每轮为当时源码的独立结果，不累加，不以较早通过替代最终验证。

| 顺序 | 实际结果 | 日志与说明 |
| --- | --- | --- |
| 取消回归 RED | 1 failed，退出 1 | [01-cancel-red.log](01-cancel-red.log)：新执行边界不存在，ModuleNotFoundError；这是新增能力 RED，并非旧 API 已可调用的行为回归 |
| 最小取消 GREEN | 1 passed，退出 0 | [02-cancel-green.log](02-cancel-green.log)：复用现有身份/lease，并拒绝取消态；有效提交此时尚未实现 |
| 有效/幂等 RED | 1 failed / 1 passed，退出 1 | [03-valid-red.log](03-valid-red.log)：有效路径仍为未实现 |
| 有效路径初版 | 2 failed，退出 1 | [04-valid-green-initial.log](04-valid-green-initial.log)：公开 model_dump 隐藏 snapshot tenant/私有 scope，不能直接重验 |
| 有效/跨 epoch 幂等 GREEN | 2 passed，退出 0 | [05-valid-green.log](05-valid-green.log)：使用 internal_payload，修正合成规则 mode 使其与 Job 契约一致 |
| COMMIT 入口撤权 RED | 4 failed / 2 passed，退出 1 | [06-commit-authority-red.log](06-commit-authority-red.log)：真实 before_commit 注入目录、actor Policy、worker Policy 撤销和到期，旧实现均错误提交，DID NOT RAISE |
| COMMIT 入口 GREEN | 6 passed，退出 0 | [07-commit-authority-green.log](07-commit-authority-green.log)：增加同步当前授权/期限复核 |
| 其余逐项行为 | 32 passed，退出 0 | [08-behaviors.log](08-behaviors.log)：await 撤权、重复读取仍重查、五阶段故障回滚、task cancellation、绑定与旧 epoch、内容冲突 |
| 独立实例竞争初版 | 1 failed / 2 passed / 32 deselected，退出 1 | [09-races.log](09-races.log)：旧执行回滚/新 claim 已通过，但测试未恢复预授权 wrapper，错误断言新 epoch 仍等于旧 snapshot；修正夹具，不放宽授权 |
| 竞争及限制补充 | 37 passed，退出 0 | [10-fencing-targeted.log](10-fencing-targeted.log)：含竞争、取消先于锁、业务提交后响应失败和实际 campaign 资源 Policy |

静态中间失败均保留：[lint-initial.log](lint-initial.log) 有 SQL 行长、Literal 推断和 JSON 字典类型问题（Ruff 退出 1、mypy 退出 1）；[lint-second.log](lint-second.log) 为测试 SQL 行长（Ruff 退出 1），mypy 已退出 0。最终修正后验证如下。

## 最终命令与结果

所有 uv 命令使用 `UV_CACHE_DIR=.artifacts/uv-cache UV_OFFLINE=1 UV_NO_SYNC=1`，未同步或安装依赖。

| 检查 | 最终结果 | 证据 |
| --- | --- | --- |
| 11 个相关文件的 pytest，排除 live/enterprise/performance | **116 passed / 1 warning**，55.28 秒，退出 **0** | [targeted-final.log](targeted-final.log)，完整参数/进程耗时见 [targeted-result.json](targeted-result.json) |
| Ruff format --check，8 个源文件及 1 个测试文件 | **9 files already formatted**，退出 **0** | [lint-final.log](lint-final.log) |
| Ruff check，同上 | **All checks passed**，退出 **0** | [lint-result.json](lint-result.json) |
| mypy，8 个受影响源文件 | **Success: no issues found in 8 source files**，退出 **0** | [lint-final.log](lint-final.log) |

最终定向在 37 项切片通过后追加当前角色变化、Job deadline 和拒绝非 SQLite/错配装配用例，与原 lease/draft/ledger/input/launch/Policy 相关回归一起运行；不把各轮数字相加。唯一告警为导入既有测试夹具引入的 Starlette/httpx 弃用提示。

本轮未运行完整套件、构建、smoke、Live/Enterprise/Performance、新 PostgreSQL 或真实飞书；完整套件由外部 orchestrator 独立运行。本助手未执行 commit、push 或任何 remote write，未触发 GitHub Actions，CI 状态为本轮未运行。源码指纹和精确变更文件见 [source-snapshot.json](source-snapshot.json) 与 [changed-files.md](changed-files.md)；文档检查见 [documentation-checks.json](documentation-checks.json)。

## Orchestrator 独立验证

实现完成后独立复验，未依赖其自报；源码范围无额外修改。

| 检查 | 实际结果 | 证据 |
| --- | --- | --- |
| 完整 Community | **1268 passed / 9 deselected / 5 warnings**，550.19 秒，退出 0 | [independent-community.log](independent-community.log) |
| Security | **309 passed / 968 deselected / 1 warning**，215.22 秒，退出 0 | [independent-security.log](independent-security.log) |
| 全库静态 | **384 files formatted、Ruff clean、192 source files mypy clean**，退出 0 | [independent-lint.log](independent-lint.log) |
| 独立只读复审 | codex-auto-review 未发现限定切片 P0–P2 问题；独立 takeover/cancel 探针 **2 passed** | [independent-review.md](independent-review.md) |
| wheel/sdist + 安装包验证 | 构建成功；全新 venv 锁定依赖安装、2 新模块导入、CLI 0.1.0、**8 个 wheel verifier**、wheel strict type consumer 均退出 0 | [independent-package-network-retry.log](independent-package-network-retry.log)、[逐命令结果](independent-package-retry-results.json) |

完整门禁相对 1228 基线增加 **40 项**，不与 116 项定向或 309 项安全相加。完整进程耗时/退出码见 [independent-results.json](independent-results.json)。五项完整套件 warning 为既有 Starlette/httpx 弃用及 SQLite/Alembic FK 反射提示。源码 SHA-256 见 [独立指纹](independent-source-fingerprints.json)。

打包首次离线安装因缓存缺 cryptography 失败，默认缓存重试仍失败；保留 [首次失败](independent-package.log) 与 [缓存重试](independent-package-cache-retry.log)。替代方案仅在新的隔离 venv 中，按现有 uv.lock 导出并下载锁定依赖后安装 wheel；未改 pyproject/uv.lock、用户凭证或用户数据库。uv build 的缓存目录提示保留；不把“离线构建成功”误称为“离线隔离安装成功”。

只读查询既有 Actions `39726798829` 与 `39726798587` 均已 completed/success，head 为原 `7e804ed`，不是本轮未提交代码的 CI。未重跑或触发 CI，未 commit/push。本轮仍不关闭 T02/T03/T04/Core，不宣称 PostgreSQL/S2、Graph、外部调用或通用 reservation 已安全接管。

## 会话外 HEAD 变化

最终证据脚本的基线断言失败，发现 HEAD 已从起始 `7e804ede91eb2e38a450f2413fefa17b9e50d897` 切换为 `64cc69287dd6d9e7eab1e2d164f852bfef56186b`。只读 reflog 显示会话外 checkout 到 master，目标为已有 merge commit。本助手未运行 checkout、commit、fetch、pull、push 或任何远程写入，也未回退该变化。

两提交的 tree ID 完全相同，`git diff 7e804ed HEAD` 为空，受测源码和文档没有因此变化，故不重复测试。[基线断言失败记录](evidence-head-check-failure.log) 与 [source-snapshot.json](source-snapshot.json) 分别保留异常和等树指纹；这里的“未提交/未推送”仅描述本助手操作，不冒充对其他进程操作的保证。
