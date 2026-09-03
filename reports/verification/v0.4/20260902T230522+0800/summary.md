# V0.4-T01 合成分析数据验证卡

```yaml
run_id: "20260902T230522+0800"
version: "V0.4"
task_id: "V0.4-T01"
depends_on: ["V0.3-Core"]
verification_level: "C"
commit: "e8b9db4 + working tree"
executed_at: "2026-09-02T23:05:22+08:00"
environment: "Darwin 25.6.0 x86_64 / Python 3.11.15 / uv 0.12.6 / uv.lock sha256:b45a0bb6cef787031c371e3351d8bbad6b7b325388eb838c6265dc46ec5ad425"
provider_model: null
config_fingerprint: null
dataset_version: "scenario_b_synthetic_v1"
eval_fingerprint: null
commands:
  - "make test（修改前 V0.3 基线）"
  - "uv run pytest tests/unit/test_v04_attribution_models.py tests/integration/test_v04_synthetic_analytics.py tests/security/test_v04_attribution_data_isolation.py -q"
  - "uv run ruff format --check ."
  - "uv run ruff check ."
  - "uv run mypy src/oria"
  - "make test（修改后完整 Community 回归）"
  - "make build"
  - "make smoke"
  - "unzip -l dist/oria-0.1.0-py3-none-any.whl | rg 'oria/(analytics|eval/attribution_data)'"
artifacts:
  - "dist/oria-0.1.0-py3-none-any.whl（gitignored）"
  - "dist/oria-0.1.0.tar.gz（gitignored）"
evidence_refs:
  - "src/oria/analytics/models.py"
  - "src/oria/analytics/schema.py"
  - "src/oria/eval/attribution_data.py"
  - "tests/integration/test_v04_synthetic_analytics.py"
  - "tests/security/test_v04_attribution_data_isolation.py"
assertions:
  - "修改前 V0.3 基线为 597 passed, 1 deselected"
  - "固定 seed 两次生成的 metadata、180 条漏斗事实、3 条活动事实和 180 条大盘事实逐行一致"
  - "漏斗阶段计数、活动时间窗和大盘转化满足 Pydantic 与 SQLite 双层约束"
  - "合成事实明确记录 source=synthetic、contains_real_entities=0、CC0-1.0、schema/generator/seed"
  - "华东正餐局部异常、活动结束时间和稳定大盘上下文均可从查询库回查"
  - "根因 code、golden rationale 与 attribution_labels 只存在于独立评测库；查询库不可查询标签表"
  - "生产 analytics 包不导入 oria.eval 标签模块"
  - "修改后完整 Community 回归为 605 passed, 1 deselected"
  - "Ruff format、Ruff lint、mypy、wheel/sdist 构建和 CLI smoke 通过"
result: "passed"
blocked_by: []
known_limits:
  - "本卡只证明 V0.4-T01 合成数据与标签隔离，不证明 T02 查询工具、T03 Agent、T04 冻结 Golden 或 T05 Live 质量"
  - "数据均为仓库内确定性合成数据，不是企业 DMS、活动或大盘数据"
  - "V0.3-T09 因当前进程无 DEEPSEEK_API_KEY 仍未运行；历史 Live 卡未替代该卡"
```

## 结果摘要

V0.4-T01 已完成。生产可查询库只包含 `analytics_metadata`、`funnel_daily`、`activity_windows` 与 `market_daily`；根因标签和人工 rationale 写入另一 SQLite 文件中的 `attribution_labels`。生成器拒绝两个逻辑库指向同一路径，并用固定 seed 生成可逐行复现的多租户、多区域、多品类时间序列。

本次未实现任何查询 Tool 或归因 Agent，也未执行真实模型、企业系统或性能验证。全量测试出现的 4 条 SQLAlchemy migration warning 与修改前基线一致，本任务未改变 V0.3 migration。
