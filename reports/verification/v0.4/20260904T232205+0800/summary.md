# V0.4-T04 Golden/Eval 进行中验证卡

```yaml
run_id: "20260904T232205+0800"
version: "V0.4"
task_id: "V0.4-T04"
depends_on: ["V0.4-T01", "V0.4-T03"]
verification_level: "CT | IT | SEC | E2E-F"
baseline_commit: "825cda1 + working tree"
executed_at: "2026-09-04T23:22:05+08:00"
environment: "macOS / Python 3.11.15 / uv 0.12.6"
provider_model: null
dataset_version: "scenario_b Golden v1 draft / scenario_b_synthetic_v2 facts"
result: "blocked"
blocked_by:
  - "50 条案例尚未由真实人工逐例审阅并批准"
  - "Holdout 尚未冻结，因此不得创建或提交 baseline"
known_limits:
  - "Fixture replay 只证明评测机制，不证明真实模型归因质量。"
  - "未运行 V0.4-T05 Live、人工/judge 校准、coverage-risk、置信度分桶或重复采样。"
  - "未运行 Enterprise、Performance、真实网络或企业 Adapter。"
```

## 已实现

- Golden manifest 绑定 30 条 development 与 20 条预留 holdout，并校验 split 数量、数据哈希和 rubric 哈希。
- 新增版本化盲评 rubric；盲评包隐藏 split、关键标记、根因标签、Golden rationale、预期工具、Provider 和架构标签。
- 新增 `oria eval run --suite attribution`，使用 MockLLM replay、合成分析数据和真实有界 attribution Graph 生成逐例报告与确定性指标。
- 新增 baseline/gate 模型和 `scripts/run_attribution_golden.py`；未批准数据会在案例执行前 fail closed，baseline 创建拒绝覆盖已有文件。
- 新增人工审阅清单和带审阅身份、带时区时间、显式全量确认的冻结入口；没有显式确认时生成器保持 `pending_human_review`。

## 验证结果

```text
V0.4-T04 定向 CT/IT/CLI: 38 passed
make lint: 275 files formatted; Ruff passed; mypy 142 source files passed
make test: 711 passed, 1 deselected, 4 warnings in 182.58s
oria eval run --suite attribution（待审数据）: exit 2, eval_blocked
run_attribution_golden.py（待审数据）: exit 1, pending actual human review
make build: wheel + sdist built successfully
make smoke: oria 0.1.0
wheel: attribution runner、rubric、gates、manifest、REVIEW 和 dataset 均已包含
git diff --check: passed
```

首次 `make lint` 只发现生成器一处可格式化换行，格式化后完整静态检查通过。首次受限环境构建因无法解析 PyPI 的 `hatchling` 失败，经允许联网后同一 `make build` 成功；失败历史未被隐藏。4 条测试 warning 是既有 SQLite/Alembic 复合外键反射 `SAWarning`。

## 门禁结论

代码、split、rubric、CLI、报告、盲评包和 baseline/gate 机制已实现并验证，但真实人工逐例审阅是不可替代门禁。当前不得把 V0.4-T04、V0.4-Core 或场景 B 真实模型质量标为通过。人工入口为 `eval/datasets/scenario_b/REVIEW.md`；批准完成后需重新生成带审阅元数据的数据集、创建 Fixture baseline、接入 `eval-golden` CI 并追加 passed 验证卡。
