# V0.3-T08 故障注入与 Core 验证报告

```yaml
run_id: "20260902T083103+0800"
version: "V0.3"
task_id: "V0.3-T08"
depends_on: ["V0.3-T07"]
verification_level: "F | C"
commit: "9c6bc0c（工作树含本报告所述未提交变更）"
executed_at: "2026-09-02T08:31:03+08:00"
environment: "macOS / Python 3.11.15 / uv 0.12.6 / uv.lock sha256:b45a0bb6cef787031c371e3351d8bbad6b7b325388eb838c6265dc46ec5ad425"
provider_model: null
config_fingerprint: "community+demo；每个测试使用隔离临时数据目录"
dataset_version: "demo-campaign-rules@1.0.0 + synthetic Scenario A fixtures"
eval_fingerprint: null
result: "passed"
blocked_by: []
known_limits:
  - "未运行 V0.3-T09 DeepSeek Live、Enterprise 或 Performance"
  - "Mock Adapter 不证明真实券、招商、商品库、选品、C 端或 IM 接入"
  - "SQLite 单 worker 结果不证明 PostgreSQL 多 worker/fencing 语义"
```

## 交付结果

- 新增 `tests/integration/test_v03_fault_injection.py`，覆盖审批后进程重建、外部受理后本地落账前退出、超时转 unknown 且不重调，以及通知局部失败不回滚 C 端投放。
- 复用既有 launch recovery 证据验证券已物化但 saga checkpoint 未推进时读取 ledger 历史继续，物化调用数保持 1。
- 新增 `docs/security/V0.3场景A威胁模型.md`，明确写 Tool、审批、外部事件、两库事务、未知副作用、Mock/Live 声明边界与 V0.6 残余风险。
- 修正架构、路线与 README 的进展漂移：V0.3-T01–T08 与 Core 已通过，T09 必需 Live 卡仍未运行。

## 五类故障点断言

| 故障点 | 实际结果 |
| --- | --- |
| LaunchPlan 批准后执行进程退出 | 重建 Service 后从持久审批完成 saga；物化券与招商发布各调用 1 次，重复执行仍各 1 次 |
| 券外部成功但 saga checkpoint 未推进 | 读取 materialize ledger 历史后继续；不重复物化券 |
| 选品提交外部受理后、本地成功落账前退出 | 首次重放保持 executing/waiting；超过 5 分钟转 unknown/reconciliation；Adapter 调用总数 1 |
| C 端投放外部受理后、本地成功落账前退出 | 同上；禁止对 terminal ledger 再执行 Adapter |
| 商家通知局部失败 | placement 保持 published；1 条 sent、1 条 dead_letter；对应 ledger 为 1 succeeded、1 failed |

## 验证命令与真实结果

```text
$ UV_CACHE_DIR=.artifacts/uv-cache make lint
240 files already formatted
All checks passed!
Success: no issues found in 125 source files

$ UV_CACHE_DIR=.artifacts/uv-cache uv run pytest -m "unit or contract" -q
400 passed, 194 deselected in 18.28s

$ UV_CACHE_DIR=.artifacts/uv-cache uv run pytest -q tests/integration/test_v03_fault_injection.py
4 passed in 2.75s

$ UV_CACHE_DIR=.artifacts/uv-cache make test
597 passed, 1 deselected, 4 warnings in 154.51s

$ UV_CACHE_DIR=.artifacts/uv-cache uv run pytest -q -m security
99 passed, 499 deselected in 27.18s

$ UV_CACHE_DIR=.artifacts/uv-cache make build
Successfully built dist/oria-0.1.0.tar.gz
Successfully built dist/oria-0.1.0-py3-none-any.whl

$ UV_CACHE_DIR=.artifacts/uv-cache make smoke
0.1.0
```

首次受限构建因沙箱内无法解析 PyPI 的 `hatchling` 构建后端而失败；经批准联网后同一命令成功。`uv` 提示项目内缓存目录可能进入制品，随后实际检查 wheel/sdist 清单，均未包含 `.artifacts` 或 `uv-cache`。

4 条 Community warning 是既有 SQLite/Alembic downgrade 反射复合外键时的 `SAWarning`；对应 migration 测试全部通过。本轮未删除或覆盖这些警告历史。

## 声明边界

- 允许声明：V0.3 Community Core、10 步场景 A、本地跨进程作用域恢复、最小权限、双来源汇聚、动态确认链、异步选品恢复及五类故障点已通过。
- 禁止声明：V0.3 全部验证通过、V0.3 DeepSeek Live 已通过、真实企业 Adapter 已接入、多 worker Durable Job 已验证。
- 下一门禁：V0.3-T09 仅验证真实 DeepSeek 在 EligibilityPolicy 候选集内软排序/生成草案，且没有直接业务写路径；它不能替代任何企业 Adapter 卡。
