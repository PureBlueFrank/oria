# V0.6-T03：Job 持久化受理增量

日期：2026-10-22，Asia/Shanghai。任务依赖：V0.6-T01/T02 的代码、schema 与契约，沿用详细路线 §1.4。**本增量不关闭 T03 或 V0.6 Core Gate**。Fixture、SQLite Community/Security/Recovery 记录如下；新 PostgreSQL、真实飞书、Live/Enterprise/Performance 未执行。

## 已实现

- `POST /v1/sessions` 为当前可信主体建立会话；`POST /v1/jobs` 接受有界 campaign/attribution 类型化槽位。服务端生成 tenant/owner、job/thread/run、预算与时间；保存稳定 requester 引用、规范业务槽位及摘要，不保存 bearer、claims、角色快照或原始聊天提示词。
- 强制 `Idempotency-Key`，只保存 SHA-256。tenant+内部主体唯一范围，同键同内容返回原 Job 当前状态，同键换内容 409；数据库唯一约束保证并发只建一个 Job/初始事件。
- 自有 Job 查询与协作式取消；queued/waiting/retry 可取消，running 只置请求取消，重复调用返回当前状态。完整快照 CAS 防止陈旧终态覆盖取消；非法状态边、不可变绑定变化、attempt/epoch 计数不符均拒绝。
- Job 和递增 sequence 的状态事件在同一事务内保存；事件或提交前身份/Policy 检查失败会全部回滚，不占用幂等键。事务内 Policy 拒绝先回滚，再记录脱敏拒绝审计，避免 SQLite 第二写连接争锁。预先授权审计不代表业务提交成功。
- tenant+所有者隔离，额外校验既有场景权限；未知、他人和跨租户资源统一 404。客户端不能提交 tenant/role/checkpoint/wait/lease/status。公开投影只含安全元数据，内部提交与执行引用不返回。
- `platform_0013` 新建 sessions/jobs/job_events，保留既有审批；复合所有者外键、幂等唯一键、状态/预算约束与 PostgreSQL FORCE RLS。任一新表非空即拒绝降级，不删除数据；PostgreSQL 还须证明读取不受 RLS 过滤。
- 服务端 `job_service` 严格布尔开关默认关闭，需显式运行时配置，共享审批服务生命周期。当前仅允许 Mock/Fixture+SQLite；启动只读检查、不迁移用户库。HTTP 不执行 Graph，health 明示 `admission_only`。

设计与操作见 [ADR-014](../../../../docs/adr/ADR-014-durable-job-admission.md)、[ADR-025 提案](../../../../docs/adr/ADR-025-job-lease-and-fencing.md)和[使用指南](../../../../docs/guides/durable-jobs.md)。

## 环境与检查

基线提交 `02261a59e46927eedfaf1a9518c282bd256f8276`。开始时已有未提交的 T02 本地交互改动，本轮在其上继续，未重置或提交。T02 的历史源码摘要、失败及 1151 项通过记录仍保留在原[验证卡](../20261021-t02-approval-interactions/summary.md)，不改写为当前结果。当前源码/依赖指纹另记 [source-snapshot.json](source-snapshot.json)。

Python 3.11.15、uv 0.12.6，使用已有锁定 `.venv`，未改依赖。命令均设置 `UV_CACHE_DIR=.artifacts/uv-cache UV_OFFLINE=1 UV_NO_SYNC=1`；本机回环测试在获准后运行，无真实 Provider/业务请求。各轮独立记录，不累加通过数。

| 检查 | 结果与证据 |
| --- | --- |
| 完整本地门禁 | **1179 passed / 9 deselected / 5 既有 warnings**，373.76 秒，退出码 0；[regression.log](regression.log) |
| 新增 Job 测试 | **28 passed**，21.45 秒；[targeted-final.log](targeted-final.log) |
| 格式/Ruff/mypy | 375 文件格式、187 源文件类型检查通过；[lint-final.log](lint-final.log) |
| 身份安全回归 | 14 passed；[identity-contract.log](identity-contract.log) |
| wheel/sdist | `make build` 成功；[build-initial.log](build-initial.log) |
| 包与导入 | 10 个模块与源码一致，全部迁移摘要正确，wheel/sdist 无工作区缓存；从 wheel 直接导入验证 heads 0013/0011；[package-content.json](package-content.json)、[wheel-import.json](wheel-import.json) |
| CLI | `make smoke`，0.1.0；[smoke.log](smoke.log) |

测试直接覆盖：同键并发、重复取消、内容冲突、跨主体/租户、伪造 authority 字段、参数边界、场景权限、事件失败/撤权回滚、Policy 拒绝审计、陈旧快照拒绝、等待/重试取消、独立解释器持久读取、应用关闭后重启及有数据拒绝降级。内部种入 running/waiting 只验证状态与取消，**不是 Graph 执行或 worker 恢复测试**。

失败历史：首批 17 passed 后补充验证场景；扩展集合 47 passed/1 failed，测试客户端默认来源不满足 local profile 的 loopback 检查，预期 503 实得 401。改为显式回环测试来源，保留原鉴权顺序与实现，最终新增集合 28 passed。见 [targeted-first.log](targeted-first.log)、[targeted-expanded.log](targeted-expanded.log)。初次 mypy 的 RowMapping 类型转换及后续测试行长问题均已修正，保留 [mypy-initial.log](mypy-initial.log)和[lint-initial.log](lint-initial.log)。

首轮完整门禁为 1178 passed/1 failed/9 deselected/5 warnings，唯一失败是旧 Job 占位路由仍断言未认证 GET 返回 404；新路由实际先鉴权返回 401。更新该断言并补充已认证但未装配服务返回 503，没有放宽实现。身份安全定向 14 passed 后重新运行完整门禁，最终 1179 passed；首轮见 [regression-initial.log](regression-initial.log)。该测试格式检查发现换行差异，格式化后末次静态检查通过，原结果见 [lint-identity-format.log](lint-identity-format.log)。文档链接初查时尚未生成 snapshot，生成后通过，保留 [documentation-initial.json](documentation-initial.json)与[documentation-checks.json](documentation-checks.json)。五项既有告警为 Starlette/httpx 弃用和 SQLite/Alembic 外键反射提示。日志仅脱敏用户/临时路径与行尾空白；检查结果见 [checks.json](checks.json)。

## 未完成与证据边界

本轮只实现 T03 的受理/状态存储基础。两场景 Graph 适配、飞书槽位追问与确认、chat/thread/source_message 绑定、既有 external_waits/inbox 到新 Job 的审批/事件恢复尚未接通。没有 claim、heartbeat、自动 retry/timeout、worker runner、执行边界 fencing 或 orphan 清理；预留列和完整快照 CAS 不证明多 worker 安全。T04/T05 的进程接管、可靠通知、SSE/Webhook 继续保留。

2026-10-17 PostgreSQL 与 10-18 原真实飞书批准结果仅作历史范围证据，不能覆盖新 revision、JSON 2.0 表单/分页或 Job 执行。新目标未选择、未运行；没有使用真实数据、发送消息、开放公网、改变外部权限或升级用户数据库。未提交或推送，因此没有触发 GitHub Actions。

当前优先级：T03 两场景 Graph/会话适配 → 审批/外部事件到 Job 的安全恢复 → T04 lease/fencing worker → T05 可靠通知/事件流。T02 新增真实交互及新 revision PostgreSQL 独立验收；V0.5-T07 仍为 29/80，本轮不恢复 Live。此前把外部验收当成 T03 实施前置的描述已在当前路线纠正，历史报告保持原样。
