---
run_id: "20260829T004513+0800"
version: "V0.1"
task_id: "V0.1-T07"
depends_on: ["V0.1-T04", "V0.1-T06"]
verification_level: "F"
base_commit: "f6d052c0834ddf7addc254eee95387cc20e75254"
commit: null
executed_at: "2026-08-29T00:45:13+08:00"
provider_model: "Scenario A deterministic replay provider v1；未调用真实 Provider"
embedding_model: "FixtureEmbedder；未下载或推理真实 BGE"
commands:
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv run python scripts/validate_scenario_a_dataset.py"
    result: "30 cases / 30 critical；status=approved"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv run python scripts/run_scenario_a_golden.py --create-baseline --output .artifacts/eval/scenario_a_v1_initial.json"
    result: "30/30 passed；case/critical/outcome/tool-sequence/grounded-proposal 五项指标均为 1.0；首个 baseline 已创建"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv run python scripts/run_scenario_a_golden.py --output .artifacts/eval/scenario_a_v1.json"
    result: "30/30 passed；与 committed baseline 候选零回归"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache make lint"
    result: "116 files formatted；Ruff All checks passed；mypy Success: no issues found in 74 source files"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache make test"
    result: "174 passed（not live / not enterprise / not performance）"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache make build"
    result: "sandbox 内因 PyPI DNS 受限失败；按权限流程联网重试后成功构建 wheel 与 sdist"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache make smoke"
    result: "oria 0.1.0"
  - cmd: "临时隔离 venv 安装 wheel 并运行 scripts/verify_t07_wheel.py"
    result: "Prompt、Graph、Checkpoint 租户隔离与 Scenario A Golden contract 全部通过"
assertions:
  - id: "Scenario A Golden review"
    covered: true
    note: "30 条 case 均记录 reviewed_by=FrankLee 与带时区 reviewed_at，manifest 为 approved/human_review_complete/baseline_created。"
  - id: "eval-golden deterministic harness"
    covered: true
    note: "每条 case 通过真实 Oria 有界 Graph 执行；覆盖标准提案、硬资格伪造、缺失/冲突、引用伪造、权限、写工具注入、非法参数和无进展。"
  - id: "eval baseline/gates"
    covered: true
    note: "dataset hash、runner/prompt/provider/embedding/tool schema 身份进入 baseline；全部 critical 与五项注册指标要求 1.0，allowed_regression=0。"
  - id: "CI eval-golden"
    covered: true
    note: "ci.yml 已增加独立 eval-golden job 和脱敏 per-case artifact 上传；本地等价命令已通过。"
result: "passed"
blocked_by: []
known_limits:
  - "GitHub Actions 尚未在本次未提交变更上远端实跑；不将本地结果写成远端 CI 通过。"
  - "验证等级为 F；真实 DeepSeek/BGE 仍未运行，不构成 Community Real 或 Live 证据。"
  - "当前变更未提交，故 commit 保持 null；baseline 为待与本次变更一同提交的 committed baseline 候选。"
---

# V0.1-T07 收口验证报告

## 结论

V0.1-T07 已完成 F 等级收口。30 条 Scenario A Golden 获得实际人工批准，首个 baseline、独立 deterministic harness、零回归 gates 与 `eval-golden` CI job 已落地。本地逐例执行 30/30 通过，全量 Core 为 174 passed，构建、CLI smoke 和已安装 wheel 门禁通过。

V0.1 版本仍为进行中：T08/T09 未完成，Core Gate 未运行；T10 真实 DeepSeek+BGE Live 卡未运行。

## 安全与真实性边界

- `sa-v1-027` 对写工具提示词注入整批 fail closed，不执行任何工具。
- `sa-v1-028` 可继续生成安全只读提案，但停业/非资格的 `demo-m003` 与 denylist 的 `demo-m004` 必须排除。
- Fixture 结果只证明确定性行为、引用与权限边界，不代替真实模型或真实 BGE 质量验证。
