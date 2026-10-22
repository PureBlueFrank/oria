# V0.6-T03：两场景 Graph 输入适配

日期：2026-10-22，Asia/Shanghai。接续 [Job 持久化受理](../20261022-t03-job-admission/summary.md)，该增量完整本地门禁为 1179 passed。本卡记录后续输入适配和其触发的离线 Demo 缺陷修复；**不关闭 T03/Core，不代表持久化 Job 已开始执行或恢复**。

## 实现与边界

`oria.orchestrator.job_inputs` 复用现有 `initial_scenario_a_state` 与 `initial_attribution_state`，不复制 CLI 业务编排，不新建 Agent。适配函数只生成 state，不调用模型、Graph、数据库或通知；不修改传入的 Job、Context、模板，不推进 Job 状态，不生成 checkpoint/lease 执行凭证。

- 重新验证 Job 快照与 request hash；tenant、所有者、session/thread/run/job 标识必须与 Context 一致，executor 必须为同租户 service。仅接受全新 queued、attempt/epoch=0、无取消、无 accepted pointer 的任务；恢复 Context 也不能重置成初始 state。
- 招商显式接收完整服务端 ScenarioAWorkflowRequest 模板，继续由原服务执行物料/补偿/选品/投放/通知策略。确认槽位生成规范请求并设置候选上限、报名模式，业务 ID 和幂等键稳定派生自 Job；effective_at 使用提交时刻。模板两个等待期限须晚于该时刻且不超过 Job timeout，超出拒绝，不自动选择策略或静默改期限。用户城市/类目作为业务需求，不扩大服务端投放或通知授权。
- 归因复用原 ResearchSpec/固定版本 Prompt，以枚举指标、有界时间和区域/类目编码生成规范问题，绑定当前 tenant。实际执行仍需显式装配原只读 AnalyticsQueryStore、五工具 registry、预算与当前 Policy；适配不提供数据或猜测结论。
- 回归复现 DemoMock 固定 `query_merchants.limit=10`：候选上限 7 时被既有 Graph 参数门禁拒绝，无法生成草案。改为遵守 Graph 给出的工具 schema 上限（仍以 10 为离线 Demo 最大请求量），保留门禁；以 1、7、10 家验证候选有界且进入审批。

本轮没有新依赖、环境变量、迁移或默认执行开关。HTTP 仍为 `admission_only`，飞书文本 Job 接收器尚未配置。输入 state 不能替代 claim/fencing、当前身份重载或执行前鉴权；实际调度与审批/外部事件恢复仍未接通。

## 检查与证据

沿用前一张卡的 Python 3.11.15、uv 0.12.6、锁定 `.venv`，使用可写隔离 cache，并显式 `UV_OFFLINE=1 UV_NO_SYNC=1`。基线及前序未提交改动不重置。当前代码/测试及配置 hash 见 [source-snapshot.json](source-snapshot.json)，前卡和历史失败结果保持原样，各轮不累加。

| 检查 | 结果与证据 |
| --- | --- |
| 完整本地门禁 | **1203 passed / 9 deselected / 5 既有 warnings**，389.78 秒，退出码 0；[regression.log](regression.log) |
| 扩展定向 | **42 passed**，34.90 秒；当时 23 项新增输入测试及 19 项既有两场景验证，[targeted-expanded.log](targeted-expanded.log) |
| 静态检查 | 378 文件格式、Ruff、188 源文件 mypy 通过；[lint-verified.log](lint-verified.log) |
| 构建与 wheel | 离线 wheel/sdist 构建与新模块直接导入、迁移完整性通过；[build.log](build.log)、[package-content.json](package-content.json) |
| CLI smoke | 0.1.0；[smoke.log](smoke.log) |

新增测试最终为 24 项：Context 各标识/owner/tenant/service executor 拒绝、已执行/取消/恢复状态拒绝、快照篡改、期限越界、错误场景、模板策略保留及稳定 ID；5 项 Graph 输入 E2E-F 覆盖招商 1/7/10 上限到达真实审批 interrupt、归因按观测选择 activity/market 两条路径及 insufficient 弃答。最后补充的 human executor 拒绝用例纳入最终完整门禁，不把此前 42 项当作包含它。

E2E-F 使用真实现有 Graph/领域服务、临时 SQLite、Mock/Fixture 与自适应合成 Provider。归因 registry/Provider 注入仅在测试中，生产 Runtime 仍封闭；不因测试结果而扩大默认工具。招商测试只从适配 state 运行既有 Graph 到首个审批，未通过 Job runner 消费数据库队列；Job 值仍为 queued，不能把它写成 Job 状态恢复证明。

首轮为 19 passed/1 failed，失败正是上述候选上限 7 与固定 Demo limit=10 冲突；修复前日志 [targeted-initial.log](targeted-initial.log) 保留，修复后扩展检查通过。初始和末次静态日志分别保留；期间修正测试 import/格式，未改变门禁。没有选择或运行真实模型、飞书、PostgreSQL 新 revision、Enterprise/Performance，无外部写入、用户数据迁移、Git 推送或 GitHub Actions。

本轮在前一受理增量 28 项基础上再新增 24 项，共新增 52 项；最终完整门禁 1203 项全部通过，数字不与前轮相加。五项既有 warnings 为 Starlette/httpx 弃用及 SQLite/Alembic 外键反射提示。日志只脱敏用户/临时目录并清理行尾空白，失败历史不删除。机器可读结果见 [checks.json](checks.json)，文档一致性见 [documentation-checks.json](documentation-checks.json)。

下一优先项是当前主体恢复与 lease/执行时鉴权协议，随后把初始输入接到受控调度、会话确认及审批/外部事件恢复。独立 worker 接管、fencing 全链路、可靠通知/SSE 继续按 T04/T05 验收；V0.5-T07 Live 仍为 29/80。
