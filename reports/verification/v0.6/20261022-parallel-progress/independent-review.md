# 并行增量独立复审

- 日期：2026-10-22（Asia/Shanghai）。
- 复审者：独立子 agent `progress_audit`，未实现或修改源码、测试与公共文档；仅写本报告。
- 范围：`core/dispatch.py`、`core/job_scheduling.py`、`core/jobs.py`、`orchestrator/job_leases.py`、`storage/jobs.py`、新增两组测试，以及 ADR-040、ADR-025 新增调度节和 ADR 索引。
- 结论：源码复审发现两项接缝校验问题，均已修复并独立复验。调度增量未发现新的阻断问题；最终 ADR 与实现边界一致。
- 验证等级：**Fixture** 纯内存契约探针与测试复验；不代表生产接收端、真实 Adapter、PostgreSQL、多 worker、Live、Enterprise 或完整 Job Graph 验收。

## 发现与修复

### 1. 未经重验的复制值可以把 unknown 投影成终态（已修复）

最初的 `reconcile_dispatch` 只检查 operation/history 绑定，没有重新运行 observation 的完整值校验。合法的 `DispatchObservation(state="unknown", receipt=None, accepted_fence=None)` 经 `model_copy(update={"state": "succeeded"})` 后，会直接被投影为 `succeeded`；改成 `rejected` 则被投影为 `failed`。独立纯内存 Python 探针实际打印了这两个错误结果，而非仅根据阅读推断。

主 agent 新增 unknown 复制、嵌套 receipt、request scope 和 Job snapshot 回归，先复现失败，再在公开 helper 接缝以 `model_dump` 后重新 `model_validate` 校验完整嵌套值。修复后的无回执/无接纳 fence 终态复制值被拒绝。原始 RED 证据见 `../20261022-t03-dispatch-contract/02-revalidation-red.log`。

### 2. 宽松重验会校验归一化副本，却留下不同的调用方幂等键（已修复）

普通 `model_validate(model_dump())` 会把复制注入的 `bytes` operation key 归一化成 `str`；helper 返回 `None`，调用方原 request 的 key 仍是 `bytes`。独立探针确认两个原始 `deduplication_key` 不相等。接收方若继续用原 request 入账，同一业务字符串可能出现第二次接纳；payload 中不受检的类型复制也存在校验对象与传入对象不一致的问题。

主 agent 增加 operation key/adapter ID 字节复制与 Decimal→string payload 三项回归，先复现失败，再将 request、observation、receiver Job 和 payload 重验全部设为 `strict=True`，避免接受只能通过隐式类型转换的复制值。RED 证据见 `../20261022-t03-dispatch-contract/04-strict-revalidation-red.log`。

## 独立验证与调度审查

独立执行命令：

```bash
.venv/bin/python -m pytest -q tests/contract/test_v06_dispatch_contract.py -k 'revalidates or revalidate or coerced or coerce'
```

初次独立复验：**11 passed / 36 deselected / 3 warnings，1.27 秒，退出 0**。三个 warning 来自测试刻意构造的不符合字段类型的合成复制值，未涉及真实业务数据。主 agent 随后用 warnings-as-error 回归复现警告会携带原始参数的问题（`../20261022-t03-dispatch-contract/06-serializer-red.log`），在校验前的 `model_dump` 设置 `warnings=False`，仍以 `strict=True` 拒绝不合法值。

基于最终代码再次独立执行上述 11 项检查：**11 passed / 36 deselected，无 warnings，1.18 秒，退出 0**。本次追加复验仅针对新修复；未重复运行完整 Community。主 agent 统一保存完整门禁与最终 47 项契约测试证据，统计时区分完整测试运行与其后新增/修复的契约检查。

调度代码与测试逐项检查：

- `release_retry` 通过完整 Job 快照 CAS，使到期重试只增加一个 attempt；后续 claim 只增加 epoch，正常等待续行不消耗失败预算。
- 通用 `replace` 禁止直接把 `retry_scheduled` 变为 `queued`，不能绕过调度预算与期限校验。
- retry 必须持有当前 owner/epoch 且租约、权限与绝对期限均有效；取消状态不能重新发放重试。
- retry/expire 不推进 accepted checkpoint，超时/过期取消不重复派发业务操作。
- 状态及事件同事务；完整快照变化导致 CAS 拒绝；取消与陈旧 scheduler 快照竞争、事件故障、目录/Policy 撤权均有回滚测试。
- 调度三操作在异步 guard 前后和同步 `before_commit` 入口复核时间及当前权限；deadline 与 lease expiry 的相等边界拒绝继续执行。
- 当前实现只提供按 Job ID 显式调用的原语，没有队列轮询、自动 runner、自动 reaper 或权限独立的清理服务。`expire` 仍要求当前 requester/worker 授权；已经撤权的任务不会因本增量自动完成清理。

派发契约只声明受信接收方必须提供的边界，测试接收方是内存 conformance fixture；真实接纳需要把当前权限/租约与持久接纳线性化，并在实际副作用边界保持稳定幂等。`pending/unknown/not_found` 均不授予重投；真实 `unknown` 对账与业务落账尚未接线。

ADR-040 已明确接纳不等于完成、历史读取仍需当前授权、权威租约与持久接纳需线性化、确定 receipt 才能改变 unknown，以及本 Fixture 不证明生产安全。ADR-025 调度节已明确预算扣除位置、显式 expire 的撤权限制、无自动 worker/reaper 和不授权未知副作用重投。ADR 索引引用存在，未发现与最终代码相冲突的能力声明。

## 复审时源文件指纹

```text
5c47638687243fee3a84d70062890c07ebf486fdef7dca555b7aeb7c95ec2428  src/oria/core/dispatch.py
402278ac1b6937781dfe55db170ca6e5b284eba2f8df59882326476490038680  src/oria/core/job_scheduling.py
b05b658be6ac7da996376be117dbb1bbe080dc2dc06f4aad40a538d397ec8412  src/oria/core/jobs.py
c00dfb0ae41223f9762e9a5e2eac3679d8d0d7d9ccdbaa9c7a9a53d3ff81ab82  src/oria/orchestrator/job_leases.py
99bfe9dd2daf64f89307e476a0afc5acf231b5a1882ab2906dbcd833e67a65c4  src/oria/storage/jobs.py
592df5ef8a71feb28a6579eb647083cb6a15d607dd05022df89c9fa2334c08a2  tests/contract/test_v06_dispatch_contract.py
e577f1c360203e79d55569a674bccff62faf8fe1e43195dfb1defa993af16bb9  tests/integration/test_v06_job_scheduling.py
```

本报告不修改历史证据。当前基线 PR #5 已合入 `main` 的结论使用主 agent 本轮 GitHub 只读连接器核实结果；本地尚未刷新的 Git refs 不作为远端是否合入的依据。本轮新增实现未据此宣称已推送或具有远端 CI 结果。
