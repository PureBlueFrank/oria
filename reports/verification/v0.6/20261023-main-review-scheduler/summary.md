# main 审查与 Job 有界调度

日期：2026-10-23（Asia/Shanghai）；基于远程 main `8de1913`。用户授权本地 V0.6 推进、OCR 后提交远程 main，并明确暂不开展外部验证。

## 实现与审查结论

1. 修复 main 的租约提交缺口：claim、heartbeat、checkpoint、finish 在最后 async guard 之后可能发生撤权/到期，原实现仍提交。Repository 现强制接受同步 commit guard，在 COMMIT 入口重查当前身份、Policy 与期限。12 项回归先 RED 后 GREEN，Job 与事件一起回滚。
2. 新增显式 `JobScheduler`：单租户、有界 keyset 分页、到期 retry 释放、绝对 timeout 关闭和取消收尾。授权仍由既有 JobLeaseService 完成；每次更新使用完整快照 CAS。重启、竞争、撤权/坏记录隔离、扫描故障恢复、停止与取消有本地测试。
3. [OCR 记录](ocr-review.md)：最新 main 增量 9/9、本轮源代码 3/3 已审，范围内无未解决阻断发现。测试与文档额外人工核对。OCR 文件筛选与规则解析不等于独立模型复审或全仓逐行保证。

## 验证

- `lease-commit-red.log`：12 failed，准确复现修复前行为。
- `lease-commit-green.log`：73 passed（新增提交入口回归 + 既有 leases/scheduling）。
- `scheduler.log`：18 passed（调度新增测试）。
- 全量 `make test`：**1518 passed / 9 deselected / 5 warnings**，593.26 秒，退出 0。9 deselected 为未选择的 Live/Enterprise/Performance，不能计入通过。
- 全库格式/Ruff 通过（396 文件）；mypy 通过（197 源文件）；`make build` 的 wheel/sdist 和 `make smoke` 通过。wheel 内 3 个变更模块逐字节匹配最终源码。
- 140 个受影响文档本地链接全部存在，`git diff --check` 通过。
- 无新增依赖、迁移、环境变量；仅临时 SQLite 与合成身份/Mock/Fixture，没有操作用户业务库。

初始 uv 缓存权限与网络失败分别保留在 `lint-sandbox-failed.log`、`test-network-failed.log`；随后按 `make sync` 使用锁定环境恢复。首次格式检查缺少空行的失败保留在 `format-initial.log`，随后按 Ruff 格式化修正。最初全量诊断与修改发生重叠，不能作为冻结源码基线：`initial-diagnostic.log` 为 13 failed / 1487 passed（12 项预期 RED，1 项独立进程测试跨越方法签名变更），不能把第 13 项归因给 main；验收以代码稳定后启动的 `community.log` 为准，不删除失败历史。两份失败日志仅移除行尾空白以满足 Git 差异检查，原始输出仍保留在本机临时日志中。

## 尚未完成

本轮是 V0.6-T03 的本地增量，**V0.6 未全部完成**。轮询尚未装配 `worker run` 或默认 API，不执行 Graph，不分类模型错误，不重投业务副作用，也不恢复审批/事件等待。真实接收端原子接纳、持久 unknown 对账、其余领域 fencing、Graph/飞书会话与审批/事件恢复、独立 worker、orphan 生命周期、可靠通知/SSE/Webhook、S0–S5 与 PostgreSQL S1/S2 仍保留。上述本地实现项不能归类成“仅缺外部验证”。

T02/T03/T04/Core 不关闭；V0.5-T07 仍为 29/80。真实飞书/PostgreSQL/Live/Enterprise/Performance 本轮未运行。详细本地路线已同步，按仓库约定不强制加入被忽略的本地文档。公开 ROADMAP、ADR 与指南同步本轮能力边界。
