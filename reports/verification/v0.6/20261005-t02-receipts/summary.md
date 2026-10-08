---
run_id: "20261005-t02-receipts"
version: "V0.6"
task_id: "V0.6-T02"
verification_level: "contract | SQLite community | security | recovery"
commit: "857dc3e plus API foundation and scoped receipt working tree"
executed_at: "2026-10-05+08:00"
result: "in_progress"
---

# T02 持久受理与去重增量

## 本轮结果

在已有 API/飞书基础上，实现 `TransactionalFeishuReceiver` 与 `platform_0010` 账本。源码及安装 wheel 均已验证 SQLite 升级；当前业务 handler 仍为合成测试实现，默认 API 未装配实际 Job/审批受理，因此不能声明 T02 全部完成或真实飞书接入。

已实现的边界：

- `ingress_receipts` 按 tenant + source/action key 唯一，`ingress_replays` 按 tenant + replay key 唯一，并以复合外键绑定受理记录。只保存稳定键、内容摘要、回执引用、时间；不保存原始 body、消息正文、身份属性或凭证。
- 受理记录先在事务内建立，使用唯一约束串行化相同请求；受信处理器的业务写入与其共用事务。未提交异常退出、处理器失败、授权拒绝均整体回滚，重投可再次受理。
- 已提交重复请求不重复执行业务写入。相同 source/replay 键绑定不同规范化内容时拒绝，API 返回脱敏 409；存储不可用返回 503。
- 等待并发写入之后重新检查身份目录和时间窗；重复请求同样调用业务授权。没有绕过撤权、审批职责分离或资源绑定的特例。
- 新增表及迁移清单、资产哈希、完整 schema/外键校验和当前 head 断言同步更新。PostgreSQL 迁移包含 FORCE RLS，但本轮没有真实 PostgreSQL 环境验证。

## 验证

| 检查 | 结果 |
| --- | --- |
| 身份/飞书/receipt/PostgreSQL 契约/RAG 定向回归 | **45 passed, 1 warning，17.38 秒** |
| 静态门禁 `UV_CACHE_DIR=.artifacts/uv-cache make lint` | **通过**；350 个文件格式正确，Ruff 通过，mypy 176 个源文件通过 |
| 最终完整非 Live/Enterprise/Performance 回归 | **1038 passed, 3 deselected, 5 warnings，279.07 秒**；`make test` 退出码 0 |
| `make build` | **通过**，sdist 与 wheel 均生成 |
| wheel 内容检查 | `platform_0010` 与源码一致；没有包含 `.artifacts` 缓存或 `.venv` |
| 隔离 wheel 安装与 `scripts/verify_t03_wheel.py` | **通过**；确认从安装包导入、当前迁移 heads、资源验证、首次初始化与重复初始化幂等 |
| 差异及文档引用 | `git diff --check` 与新增文档本地链接检查均通过 |

定向 receipt 测试覆盖：从 `platform_0009` 升级及回滚、同一消息并发重投、不同正文复用同一 source key、不同事件复用 nonce、业务事务失败回滚、独立子进程在提交前 `os._exit(12)` 后回滚、重建连接后重复去重、卡片重复受理、相同键在不同 tenant 下独立、撤销映射、策略撤权、过期请求以及持久表无原文/凭证。

独立子进程测试证明的是 SQLite 未提交事务的崩溃回滚；不是 PostgreSQL、多 worker、Job lease 或跨进程 Graph 恢复验收。合成 acceptor 只向临时表写入回执引用，不能替代实际审批动作的资源/版本/wait 绑定验证。

隔离安装只安装本地 wheel，依赖通过受控路径复用仓库已锁定的 `.venv`，没有重新解析或安装未锁定业务依赖。验证明确检查 Oria 来自隔离 `site-packages`，而非 editable 源码。

安装包的 `oria api serve --help` 入口也通过。五条全套 warning 包含四条既有 SQLite/Alembic 外键反射提示和一条 Starlette/httpx TestClient 弃用提示，未屏蔽。机器可读汇总见 [checks.json](checks.json)。

## 失败与修复记录

1. 先增加接收器回归用例，收集时因缺少 `oria.ingress.receipts` 失败；随后实现。
2. 首轮迁移用例中一个通过，一个因 migration manifest 根哈希未同步失败；同步清单和 loader 固定哈希后通过，没有关闭完整性校验。
3. 首轮 lint 发现测试内子进程脚本行宽问题，格式修复后静态门禁通过。
4. 沙箱内构建无法解析 PyPI DNS，构建工具获取失败；使用获准的网络权限重试后构建成功。构建提示仓库内 cache 可能被打包，实际检查 sdist/wheel 后确认未包含缓存。
5. 10 月 3 日最终全套进程的临时日志在本轮已不存在，未回填推测结果；本轮重新执行最终工作区全套验证。10 月 3 日已知的 RAG 指纹失败及 180 条不变对比仍保留于[历史卡](../20261003-t02-foundation/summary.md)。

## 状态与未验证范围

- T02 仍进行中：实际业务处理器、Job/审批 HTTP 路由、审批动作绑定、详情查询/翻页尚未接通，默认 callback 保持 503。不得把接收器内部接口当作已完成端到端服务。
- T03 的 Job 和 T05 的可靠出站账本未实现；接收器不执行外部副作用或自动重试未知结果。
- 账本不自动清理；nonce 到期字段存在，正式保留和清理策略须与业务幂等保留期一起落实。
- 未运行 Live、Enterprise、Performance；未发真实飞书消息、未读取密钥文件、未操作用户现有数据库。PostgreSQL E-like 保持未实测，V0.5-T07 仍为历史 29/80 暂停状态。
- 未提交、推送或创建 PR，GitHub Actions 未运行。合并前仍需 `eval-baseline-update` 标签及 CODEOWNERS eval reviewer 对既有基线指纹更新的审核。

迁移清单 SHA-256：`4d31721a3ee997dd121b11d02e8e716258aa0109b38c3594da8680e3e15c1d0e`。锁文件沿用本次 API 基础新增依赖的 `36c6f155404025b358f075c50f3b47e7b5e2da93c9066b9bfdc78a1cb5146def`；本轮没有新增依赖或再次修改 RAG 基线。

使用说明：[API 基础指南](../../../../docs/guides/api-foundation.md)。设计边界：[ADR-037](../../../../docs/adr/ADR-037-api-identity-and-feishu-boundary.md)。
