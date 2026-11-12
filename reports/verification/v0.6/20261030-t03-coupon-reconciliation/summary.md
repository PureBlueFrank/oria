# V0.6-T03 受控 Mock 券结果对账

日期：2026-10-30（Asia/Shanghai）。基线：`fda4f9f9f283789e08e06666a7755aa4dae1aca3`，通过 GitHub 只读接口核对与远程 main 一致。用户授权按优先级推进本地开发；本轮没有提交、推送或外部写入。

## 进展与选择

V0.5 Core 已完成，T07 Live 对照仍为 29/80；V0.6 T02/T03 进行中，T04/T05 未交付。沿当前路线优先补齐副作用对账，再推进 Graph/审批及事件恢复、独立 worker、可靠通知。本轮完成 **SQLite + 精确内存 Mock 的结果持久投影**，真实接收端原子接纳、持久操作历史与认证查询继续待办，没有将外部验收标为完成。

新增显式内部 `JobCouponDispatchService.reconcile(token, args, plan, approval_id)`，复用派发入口的当前身份、独立 worker、资源 Policy、租约、审批和完整业务事实校验。只读查询按原幂等键及完整参数绑定的 Mock 历史，不接受调用者提供 receipt，不调用 materialize 或 compensate。查询后和 COMMIT 入口复核权限与期限。

- reserved 拒绝对账；executing/unknown 可查询，succeeded/failed 重新校验后返回历史。
- 确定 accepted/rejected 回执将 ledger 收敛为 succeeded/failed；成功券状态、saga、ledger、Domain/Audit/outbox 在同一业务事务提交，attempt 不增加。
- 缺失/未知/异常/超时/无效回执不授予重投；已有 unknown 不新增事件，孤立 executing 收敛 unknown。
- 服务和连接重建可继续使用持久 Oria 结果；Mock 接收端重建丢失历史时保持未知，不能推断副作用未发生。

本轮沿用 ADR-025 的 SQLite 边界及 ADR-040 的未知结果语义。未实现真实 `FencedDispatchAdapter`，没有改动其接纳契约。

## 验证

环境与源码 SHA-256 见 [environment.json](environment.json)。Python 3.11、uv 0.12.6、现有 uv.lock；仅临时 SQLite、合成身份和 Mock，无真实业务数据、模型请求、新依赖或 migration。默认 uv 缓存访问被沙箱拒绝，测试改用 `UV_CACHE_DIR=/tmp/oria-uv-cache`，使用已有锁定虚拟环境。

- 新增测试阶段性结果：[32 passed](tests-focused.log)。随后仅强化“原 receipt 早于 unknown 更新时间”的时钟测试，最终源码以完整 Community 验证为准。
- 用例覆盖丢失响应、孤立 executing、成功/拒绝、服务和数据库连接重建、重复/并发、接收端历史丢失、跨 epoch、跨租户/取消/错参数/审批拒绝、查询异常/超时、伪造 receipt、查询后及提交入口身份/Policy/lease/审批版本失效、事件失败回滚与取消传播。
- [全库格式/Ruff/mypy](lint.log)：397 文件格式及 Ruff 通过，197 源文件 mypy 通过。
- [完整 Community 测试](community.log)：**1549 passed / 1 failed / 9 deselected / 5 warnings**，530.10 秒。唯一失败是既有 HTTP 重启用例在 `listener.bind(("127.0.0.1", 0))` 被沙箱拒绝（`PermissionError: Operation not permitted`）。该用例在允许本机回环端口的执行环境中[单独复验 1 passed](loopback.log)，9.34 秒。最终 1550 个选中用例分别获得通过证据，其中新增 32 项在完整测试中通过；不把首次完整运行写成零失败，也没有重跑整个测试集挑选结果。9 deselected 是未选择的 Live/Enterprise/Performance，不计为通过。
- [构建](build.log)、[CLI smoke](smoke.log)、[wheel 逐字节源码比对](wheel-source-check.log)通过。
- [文档引用检查](document-links.log)：6 份受影响文档的本地链接均存在；`git diff --check` 通过。

命令：`UV_CACHE_DIR=/tmp/oria-uv-cache uv run --offline pytest tests/integration/test_v06_job_coupon_reconciliation.py -q`、`UV_CACHE_DIR=/tmp/oria-uv-cache make test`、`UV_CACHE_DIR=/tmp/oria-uv-cache make lint`、`make build`、`UV_CACHE_DIR=/tmp/oria-uv-cache make smoke`。本轮未推送，未触发新 GitHub Actions；本地结果不代表跨平台远程 CI 已通过。

受限用例复验命令：`UV_CACHE_DIR=/tmp/oria-uv-cache uv run --offline pytest tests/integration/test_v06_feishu_server_lifecycle.py::test_configured_receiver_survives_process_restart_and_revocation -q`；只监听临时本机端口，使用合成飞书消息，不连接真实飞书服务。最终源码指纹与测试执行时一致。

## 失败历史

- [首轮测试](tests-initial.log)：4 failed / 23 passed。重建服务测试误将旧 platform session 的 lease service 与新 DatabaseResources 组合，触发既有绑定保护；修复测试装配。
- [第二轮组合测试](tests-second.log)：4 failed / 49 passed。重建测试误把 lease owner hash 用作 worker instance ID，再次触发既有保护；改为原受信实例 ID。上述失败均为新测试装配错误，没有放宽生产检查。
- [首轮 lint](lint-initial.log)：导入顺序与长行；[第二轮 lint](lint-second.log)：分支局部变量重定义，已改用不同变量名；最终静态检查通过。
- [首次构建](build-initial.log)：临时缓存缺少构建依赖，沙箱无法解析 PyPI。经工具权限复核后使用现有 uv 缓存构建通过。没有改依赖或工具链版本。
- 日志仅替换工作区绝对路径及移除行尾空白，其余保留；临时原始日志仍在 `/tmp/oria-reconciliation-*.log`。

## 剩余优先项与边界

1. T03：真实接收端原子接纳、持久操作历史、认证查询与独立目标验证；其余领域提交 fencing。
2. T03：Graph 调度与审批/事件恢复、飞书会话、调度进程装配及自动对账。
3. T04/T05：独立 worker、accepted pointer/orphan 生命周期、可靠通知、SSE/Webhook。
4. T02/T06/T07：新增真实飞书交互、PostgreSQL revision、S0–S5 与 PostgreSQL 两独立 worker S1/S2。
5. 独立卡 V0.5-T07：剩余 51 次 Live 对照和盲评；须重新明确 target、环境和预算。

没有默认 API/Graph/worker 装配、网络 Adapter、持久接收端或跨库原子性。两个独立 SQLite 连接只证明本地事务竞争，不能作为 PostgreSQL/S2 或独立 worker 验收。COMMIT 交给驱动后的外部撤权仍不保证追溯回滚。Live、Enterprise、Performance、真实飞书和用户库迁移未运行。T02/T03/T04/Core 不关闭。

历史证据引用：[main 审查与有界调度](../20261023-main-review-scheduler/summary.md)、[受控 Mock 派发](../20261022-t03-coupon-dispatch/summary.md)、[外部类型契约](../20261022-t03-dispatch-contract/summary.md)。历史结果仅用于确认依赖和范围，不计入本轮新通过数量。
