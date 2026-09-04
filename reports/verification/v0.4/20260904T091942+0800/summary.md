# V0.4-T02 只读归因 Tool 验证卡

```yaml
run_id: "20260904T091942+0800"
version: "V0.4"
task_id: "V0.4-T02"
depends_on: ["V0.4-T01", "V0.2-T03"]
verification_level: "CT | IT | SEC | C"
baseline_commit: "1651bb0 + working tree"
executed_at: "2026-09-04T09:19:42+08:00"
environment: "macOS / Python 3.11.15 / uv 0.12.6 / uv.lock sha256:f1683586845697ed1095d4266a6656f32fe8f5c41e3637101975bd4716c55075"
provider_model: null
dataset_version: "scenario_b_synthetic_v2"
result: "passed"
blocked_by: []
known_limits:
  - "本卡只证明 V0.4-T02 只读 Tool 与数据边界，不证明 T03 动态归因 Agent、T04 冻结评测集或 T05 Live 质量。"
  - "当前进程未配置任何 Provider Key；本轮未运行 Live、Enterprise 或 Performance。"
  - "真实 LLM 仍仅有历史 DeepSeek 验证卡通过，其他 Provider 不得声明为 Live 已验证。"
```

## 交付

- 新增 `AnalyticsQueryStore`，仅以 SQLite `mode=ro + query_only` 打开可查询库，并验证 schema version 与必需列。
- 新增 `query_funnel`、`drill_down`、`query_activity`、`query_market_overview`、`search_history_experience` 五个 versioned strict Tool 和独立封存注册表。
- SQL Tool 仅使用固定查询与白名单维度；tenant 只来自可信 `Context`，调用方不能提供 SQL 或 tenant。
- 时间范围最长 367 个日历日；活动查询至少绑定类目或商家，大盘支持上一周期与去年同期。
- 历史经验按 `document_kind=attribution_history` 做 dense/BM25 前置过滤，仍由现有 AuthorizedRetriever 强制 tenant/ACL，返回内容保持 `untrusted_data` 且带可回查 citation。
- 合成分析 schema 升至 v2，增加商家维度的活动绑定；根因标签仍仅存于独立 eval 数据库。

## 验证结果

```text
V0.4 定向 CT/IT/SEC: 14 passed
make lint: 261 files formatted; Ruff passed; mypy 138 source files passed
make test: 654 passed, 1 deselected, 4 warnings
make build: wheel + sdist built successfully
make smoke: oria 0.1.0
wheel contents: analytics/demo.py, analytics/query.py, tools/analytics.py present
git diff --check: passed
```

4 条 warning 是既有 SQLite/Alembic 复合外键反射 `SAWarning`，本任务未改动 V0.3 migration。构建首次因受限环境无法解析 PyPI 失败，经允许联网后原命令成功；失败历史未被隐藏。

## 结论

V0.4-T02 的查询契约、SQL 只读边界、tenant/时间范围、参数注入拒绝、证据 provenance 和历史经验 ACL/citation 已通过。下一项可进入 V0.4-T03；未实现 Agent 之前不得声明场景 B 归因能力已完成。
