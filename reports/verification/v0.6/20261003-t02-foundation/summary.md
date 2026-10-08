---
run_id: "20261003-t02-foundation"
version: "V0.6"
task_id: "V0.6-T02"
verification_level: "fixture | contract | security | local HTTP smoke"
commit: "857dc3e plus scoped working tree"
executed_at: "2026-10-03+08:00"
result: "in_progress"
---

# V0.6-T02 API 与飞书基础增量

## 结论与范围

本轮完成 API/身份/协议/展示基础增量，不代表 T02 全任务或 V0.6 Core 完成。实际没有 durable receiver、Job/审批 HTTP 路由、持久去重账本、动作资源绑定或详情翻页服务；飞书业务回调即使验签通过，也返回 503，避免丢消息却报告成功。T03 的依赖门禁尚未满足。

- `api serve`：显式回环本地身份或 JWT 配置，独立 executor，禁用 access log 和转发身份头信任；服务端 request ID、统一脱敏错误、禁止响应缓存。
- RS256/JWKS：固定算法与可信地址，签名/issuer/audience/time/subject 校验，RSA 至少 2048 位，有限缓存及刷新频率。未知 kid/失败关闭；不读取 JWT 的 tenant/role/URL 作为授权事实。
- 身份目录：每次重新加载，撤销映射立刻拒绝；资源 helper 重新核对 actor/executor，要求显式提供服务端资源属性并复用 Policy，检查跨租户和审批职责分离。
- 飞书：按官方协议验证签名、AES-256-CBC、时间窗、Verification Token、应用与平台身份，支持文本和新版 card action；生成稳定 source/action/replay 键。只定义 durable receiver 接缝，**未实现持久去重**。
- 出站：tenant/chat/subject 白名单与通知 Policy，纯文本/版本化卡片脱敏，不读取文件、不自动重发未知结果；尚未装配 token provisioning 或可靠 delivery ledger。
- IM v1：七状态 golden，五段一页、长度限制、详情编号与下一步文案。不接受任意 Tool/State 字段；不透明 action 引用仍须后续解析和鉴权。

## 验证记录

| 验证 | 结果 |
| --- | --- |
| `make sync` | 成功，uv 0.12.6；新增 FastAPI 0.142.2、PyJWT 2.15.1、cryptography 47.0.0 及锁定传递依赖，uvicorn 显式列入依赖 |
| 新增身份/飞书定向用例 | 初轮 29 passed；最终 ABAC 接缝修复后 29 passed，3.76 秒 |
| `make lint` | 格式与 Ruff 通过；347 个文件格式正确，174 个源文件 mypy 通过 |
| 首轮 `make test` | **1 failed, 1034 passed, 3 deselected, 5 warnings**，239.62 秒；RAG 基线因新锁文件指纹不匹配失败，历史保留 |
| 修复后完整回归 | 上轮中断后临时日志已不可用，未确认最终结果；10 月 5 日对最终工作区重新执行，结果见[后续验证卡](../20261005-t02-receipts/summary.md) |
| 场景 A Golden 脚本 | **45/45 passed**；[逐例报告](scenario-a-golden.json) |
| RAG Golden 脚本 | 三管线各 60 条通过原质量门禁；[逐例报告](rag-golden.json) |
| 独立进程 + 回环真实 HTTP | `api serve --local` 启动成功；health=200、me=200（executor=oria-api）、未配置飞书入口=503 且 retryable=true；临时进程已关闭 |
| `make build` | 源码包与 wheel 构建通过；最终授权接缝变更后再次构建 |
| `git diff --check` | 通过 |

首次 lint 暴露行宽、中文标点规则和可变默认值等问题，均在格式化及修复后通过。资源授权 helper 的复查发现原接口无法传入服务端 ABAC 作用域；新增回归先以 `unexpected keyword argument 'resource_attributes'` 失败，再添加必填作用域参数，验证区域不匹配拒绝；该失败不是既有版本缺陷。

五条全套 warning 中四条为既有 SQLite/Alembic 外键反射警告；一条为新增 FastAPI/Starlette TestClient 使用 httpx 的弃用提示。当前锁定组合用例通过，没有为了消除提示引入另一 HTTP 客户端或屏蔽告警。

## RAG 基线指纹更新与审核要求

首次全套回归先通过 required quality thresholds，随后在 frozen baseline identity 校验处失败。原因是本轮新增依赖改变 `uv.lock` 哈希，属于本次改动引起的问题。

使用现有 `run_rag_eval/create_rag_baseline/assert_rag_gates` 重新生成候选并逐字段比较：只允许 `lock_sha256` 和 `eval_fingerprint` 改变；三条管线 **180 个逐例记录完全相等**，指标、数据、gates、runner、创建日期均不变。验证后同步本地基线这两个字段，没有降低阈值或筛选重复结果。

- [原始基线备份](rag-baseline-before.json)
- [新旧逐例 diff（180 项）](rag-baseline-diff.json)
- [锁文件重绑定原始运行](rag-rebinding-run.json)

新锁文件 SHA-256：`36c6f155404025b358f075c50f3b47e7b5e2da93c9066b9bfdc78a1cb5146def`。

未来 PR 必须带 **`eval-baseline-update`** 标签并由 **CODEOWNERS eval reviewer** 批准；本次只是本地修改，未创建 PR、未冒充审核，也未放宽 baseline-update 门禁。

## 未运行与后续

- 无 Live Provider、Enterprise 或 Performance 执行，没有调用真实模型、真实飞书 API、真实 OIDC 或发送外部消息。新飞书测试使用合成数据与 Mock HTTP；不是企业通道证据。
- PostgreSQL：本机未发现 `docker`/`postgres` 可执行程序；沿用 T01 缺测试环境的 blocked 事实，没有读取凭证文件或声称真实 PostgreSQL 验证通过。
- V0.5-T07 保持 29/80 暂停，没有修改冻结结果或续跑付费评测。
- T02 待完成持久 replay/source/action 去重及审批动作绑定、实际详情/翻页；与 T03 受理事务衔接后再关闭门禁。真实飞书双场景 UX 按 T06 独立验收。
- 未 commit/push，未运行 GitHub Actions；本地 macOS x86_64 结果不代表 Linux 或 arm64 CI 通过。

使用与官方协议依据见 [API 指南](../../../../docs/guides/api-foundation.md)，设计边界见 [ADR-037](../../../../docs/adr/ADR-037-api-identity-and-feishu-boundary.md)。
