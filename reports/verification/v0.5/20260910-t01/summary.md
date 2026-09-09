# V0.5-T01 会话上下文治理验证卡

```yaml
run_id: "20260910-t01"
version: "V0.5"
task_id: "V0.5-T01"
depends_on: ["V0.4-Core"]
verification_level: "UT | CT | IT | SEC | C"
baseline_commit: "3349732 + working tree"
executed_at: "2026-09-10+08:00"
environment: "macOS / Python 3.11.15 / uv 0.12.6 / uv.lock sha256:f1683586845697ed1095d4266a6656f32fe8f5c41e3637101975bd4716c55075"
provider_model: null
dataset_version: null
result: "passed"
blocked_by: []
known_limits:
  - "本卡只证明会话级短期历史与上下文治理，不证明 T02 长期 opt-in Memory、TTL、删除/导出或 memory-as-tool。"
  - "Token 估算是确定性保守启发式，不是特定 Provider tokenizer 的精确计数。"
  - "本轮未运行真实网络、Live、Enterprise 或 Performance，也未新增 migration。"
```

## 交付

- 新增 `ContextBudget`、基于 UTF-8 字节的纯函数 Token 估算器，以及可 JSON 序列化的有序 `FactLedger`。
- 新增按 tenant + session 隔离的 `InMemoryMemory`；`search()` 依 T01 边界固定返回空列表。
- 溢出时保留 system 消息、最近消息后缀和规范 JSON 事实摘要；事实提取仅采用确定性结构化规则，不调用 LLM。
- 社区 runtime 默认挂载进程内 Memory，`research_model_node` 在既有 `ResearchLimits` 硬门禁之后、Provider 调用之前压缩输入。
- `ResearchState.fact_ledger` 使用 `NotRequired` 且读取时默认空列表，旧 checkpoint 可继续反序列化。

## 验证结果

```text
make lint: passed; 303 files formatted, Ruff passed, mypy 150 source files passed
non-Live suite: 938 passed, 1 deselected, 4 warnings in 313.53s
security marker: 108 passed, 831 deselected in 41.28s
V0.5-T01 targeted regression: 44 passed
fixed-fact preservation assertion: 1 passed
context-budget/token-estimator unit file: 5 passed
git diff --check: passed
```

`make lint` 首次只发现 5 个新增/修改文件需要 Ruff 机械格式化；定向格式化后重跑完整 lint 通过。完整测试的 4 条 warning 是既有 SQLite/Alembic 复合外键反射 `SAWarning`，本任务未修改 migration。

## 事实保持断言

溢出历史中固定写入并在压缩后逐项断言：

- `merchant_id = merchant-042`
- `merchant_name = 海棠商行`
- `amount = 128500`
- `conclusion = 经营异常由客单价下降导致`

测试同时断言四项值存在于结构化账本和模型可见摘要，且压缩后估算 Token 不超过消息预算。

## 边界结论

V0.5-T01 的 Fixture/Community 与安全回归通过。本结论不将进程内 Memory 表述为长期记忆，不将启发式估算表述为 Provider 精确 Token 计数，不声明 Live/Enterprise/Performance 已验证。V0.5 仍未达 Core Gate，下一依赖任务为 T02。
