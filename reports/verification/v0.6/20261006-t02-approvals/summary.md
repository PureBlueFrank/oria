# V0.6-T02 审批 HTTP 与并发保护增量

- 日期：2026-10-06；基于本地工作区（HEAD `857dc3e`），保留 10 月 3 日/5 日未提交增量。
- 任务状态：**T02 进行中，门禁未关闭**。本轮交付审批 API 库集成和底层并发修复；默认 `api serve` 尚不装配审批运行时，认证后仍返回 503。
- 范围：Fixture/Community/Security；合成身份和业务、真实 RSA 签名/SQLite，无 Live、Enterprise、Performance 或真实飞书消息。

## 交付

1. `GET /v1/approvals/{approval_id}` 按可信 tenant 读取，复用现有审批 Policy，并在详情投影前重新检查身份。返回脱敏卡片和版本指纹，拒绝不可访问资源及越界页；当前投影最多五段，只有第一页。
2. `POST /v1/approvals/{approval_id}/decision` 要求详情版本指纹，禁止客户端提交 tenant/role/checkpoint/wait；自批、过期、越权和撤权被拒绝，旧版本/重复决定返回 409。只记录审批决定，恢复执行仍走原有完整绑定校验。
3. `ApprovalRepository.replace` 新增必需的 `expected` 读取快照。SQLite 条件更新覆盖完整原记录，只有一个并发竞争者提交决定及对应 audit/outbox。内存仓库同契约，服务端所有决定/失效写入传递原快照。没有新增数据库迁移、依赖或环境变量。

## 验证与失败历史

- 并发回归先在旧代码复现：两个协程都读到 pending，均通过授权，两个决定都提交；`len(winners) == 1` 断言失败（1 failed，2.46 秒）。修复后只有一个成功，最终记录等于胜者，audit/outbox 各一条。
- 首次 HTTP 测试的审计断言误用了不存在的 `event_type` 列；按实际表契约修正为 `action`。首轮静态检查另发现导入顺序，已修正。未隐藏这些验证失败。
- 定向回归：33 passed，1 warning，4.86 秒。覆盖审批 API、真实签名、现有审批 RBAC、SQLite 仓库和并发决定；上下文构造使用轻量替身，业务授权/审批服务/数据库为真实本地组件，不宣称生产运行时装配 E2E。
- `make lint`：353 文件格式检查、Ruff、177 源文件 mypy 通过。
- 构建首次因沙箱 DNS 无法访问 PyPI 失败；经批准联网复验通过，生成 sdist/wheel，确认审批模块打包且缓存未进入发行物。
- 全量非 Live/Enterprise/Performance 回归：**1041 passed、3 deselected、5 warnings，253.21 秒**。
- wheel 安装、全新临时数据初始化/重复初始化、资源与 migration head、API CLI 帮助、审批模块导入及 OpenAPI 路径均通过。复用此前独立验证虚拟环境并重新安装本次 wheel，依赖来自项目锁定环境；不宣称空依赖缓存部署验证。
- 本地文档链接、`git diff --check` 通过；结构化结果见 [checks.json](checks.json)。全量原始输出保存于本地 `.artifacts/verification/20261006-t02-approvals/full-test.log`，不提交环境路径日志。

## 未完成与边界

- 审批 CLI 生命周期装配、飞书不透明 `action_ref` 的持久资源/版本/wait 绑定、实际受理处理器、Job/Graph 恢复和飞书详情/翻页动作仍待实现。HTTP 版本指纹不能替代这些绑定。
- 实际 PostgreSQL 并发/RLS 未运行，沿用 T01 环境 blocked；T07 Live 29/80 状态不变。
- 未迁移用户现有数据库、未提交或推送，未触发 GitHub Actions。
- 前置证据：[API 基础](../20261003-t02-foundation/summary.md)、[持久去重](../20261005-t02-receipts/summary.md)。旧失败与 Live 卡保持原结论。
