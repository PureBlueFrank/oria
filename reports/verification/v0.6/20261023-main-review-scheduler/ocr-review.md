# OCR 审查记录

日期：2026-10-23，Asia/Shanghai。使用 `open-code-review-delegate`：OCR CLI 负责文件筛选与规则解析，主助手按规则审查；没有调用外部 OCR LLM，也没有独立子代理审查。

## main 检查

基线：远程 main `8de1913`。OCR 对最新合并增量 `cbc486a..8de1913` 选出 9 项，另扩展检查租约提交路径和调用上下文。不是全仓每行审计，也不保证没有其他缺陷。

- total_files：9（preview 总变更 44，OCR 自动排除 35）
- reviewed_files：9
- skipped_files：0
- coverage_rate：100%（OCR reviewable 集合）

| path | status | 审查结论 |
| --- | --- | --- |
| reports/verification/v0.6/20261022-parallel-progress/baseline-ci.json | added | reviewed；历史 CI 快照字段自洽，非本轮 CI 结论 |
| reports/verification/v0.6/20261022-parallel-progress/documentation-checks.json | added | reviewed；历史引用检查统计 |
| reports/verification/v0.6/20261022-parallel-progress/package-content.json | added | reviewed；历史打包记录 |
| reports/verification/v0.6/20261022-parallel-progress/source-snapshot.json | added | reviewed；历史指纹和范围 |
| src/oria/core/dispatch.py | added | reviewed；嵌套重验、稳定键、未知结果不授予重投，未发现新增阻断项 |
| src/oria/core/job_scheduling.py | added | reviewed；期限、预算、取消优先和字段约束 |
| src/oria/core/jobs.py | modified | reviewed；通用 CAS 拒绝绕过 retry 预算 |
| src/oria/orchestrator/job_leases.py | modified | reviewed；发现下述既有租约路径提交缺口 |
| src/oria/storage/jobs.py | modified | reviewed；调度有 commit guard，租约缺失 |

### 已修复发现

- path：`src/oria/storage/jobs.py`
- category：security
- severity：high
- content：main 的 `replace_leased` 仅在异步 guard 前后验租约，调用 `_replace` 时未提供同步 commit guard。最后一次 guard 后到 COMMIT 入口期间身份撤销、Policy 撤销或租约到期，claim/heartbeat/checkpoint/finish 仍可能提交 Job 与事件；身份检查即使在返回时抛错也无法回滚已经提交的状态。12 个 before_commit 故障注入用例全部先失败，补齐同步检查后通过。

## 本轮改动复审

OCR workspace preview 选出以下 3 项，已按共同 Python 规则逐项审查并核对完整上下文：

| path | status | 结论 |
| --- | --- | --- |
| src/oria/orchestrator/job_leases.py | modified | reviewed；所有租约更新统一传入当前身份/Policy 的同步 guard |
| src/oria/storage/jobs.py | modified | reviewed；同步检查前后验时钟，查询固定租户、绑定参数及有界分页 |
| src/oria/orchestrator/job_scheduler.py | added | reviewed；仅状态维护，期限优先、CAS 防重复、分页推进、失败隔离、取消传播和资源宿主边界 |

- total_files：3
- reviewed_files：3
- skipped_files：0
- coverage_rate：100%
- 未解决 Critical/High/Medium：0（限上述审查范围）

OCR 默认排除测试、Markdown 和日志；额外人工核对 3 个修改/新增测试文件、ROADMAP、ADR-025、durable-jobs 指南和证据索引。没有把排除文件算作 OCR 逐项覆盖；完整排除列表见 preview 日志。新增测试覆盖真实临时 SQLite 事务回滚、事件一致性和调度恢复，不以 Mock 结果宣称 PostgreSQL、多 worker、真实网络或 Graph 已验收。
