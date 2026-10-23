---
task_id: V0.6-T03
date: 2026-10-22
levels: [Community, Security, Recovery]
result: passed
scope: explicit internal Job retry and expiry primitives on temporary SQLite
source: uncommitted working tree based on 476b034
---

# Job 显式重试与到期处理

新增 `core/job_scheduling.py` 和 `JobLeaseService.schedule_retry/release_retry/expire`。重试计划要求有效 owner/epoch、当前 requester/worker 权限、剩余预算及绝对 deadline；到期 release 通过完整快照 CAS 只扣一次 attempt，下一次 claim 保持该预算。正常等待续行不耗失败预算，通用 replace 不能绕过 retry release。expire 处理绝对 timeout 和已取消且失效的 lease，取消优先，accepted pointer 不变。

状态与事件同事务，异步边界和同步 COMMIT 入口重查当前目录/Policy/时间；异常、撤权和过期整笔回滚。expire 仍要求当前授权，被撤权任务不会自动清理。无自动轮询/worker/Graph、审批或事件恢复、错误分类、外部重投；不是 PostgreSQL 或独立 worker 接管验证。决策见 [ADR-025](../../../../docs/adr/ADR-025-job-lease-and-fencing.md)。

## 验证

- `01-targeted.log`：默认 uv cache 因沙箱权限失败，测试未开始。
- `02-targeted.log`：隔离 uv cache 缺 editable 构建依赖，网络 DNS 不可用，测试未开始。
- `03-targeted-venv.log`：使用项目已有锁定环境的 `.venv/bin/python -m pytest -q tests/integration/test_v06_job_scheduling.py`，**36 passed / 1 既有 Starlette warning，29.74 秒**。

覆盖当前/过期/异主 lease、预算和期限等号、重复/并发 release、等待态禁止恢复、取消竞争、同事务事件失败、目录/Policy 撤销以及最后 COMMIT 入口时钟/权限变化。随后只修正测试 lambda 写法和导入/格式，最终组合回归及静态检查见[本轮汇总](../20261022-parallel-progress/summary.md)，独立代码审查见[复审报告](../20261022-parallel-progress/independent-review.md)。

无 migration、依赖或环境变量变化；未执行用户库迁移、Live/Enterprise/Performance 或外部写入，T03/T04/Core 未关闭。
