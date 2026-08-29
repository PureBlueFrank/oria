---
run_id: "20260829T101609+0800"
version: "V0.1"
task_id: "V0.1-T09"
milestone_id: "V0.1-Core"
depends_on: ["V0.1-T08"]
verification_level: "F/C（Fixture + Community）"
base_commit: "f6d052c0834ddf7addc254eee95387cc20e75254"
commit: null
worktree_state: "dirty；报告覆盖当前未提交的 V0.1-T07–T09 变更"
executed_at: "2026-08-29T10:16:09+08:00"
environment:
  os: "Darwin 25.6.0 x86_64"
  python: "3.11.15（锁定虚拟环境）"
  uv: "0.12.6"
  install_mode: "source + isolated installed wheel"
provider_model:
  provider: "DemoMockLLMProvider"
  model: null
  revision: null
  request_ids: []
embedding_model: "FixtureEmbedder；未下载或推理真实 BGE"
config_fingerprint: "source sha256:1eb1e1def37c31750b445f647313984182dbbcb9d41b6b501e8eef8810b8d446; installed-wheel sha256:d39a13ac5cf114fcbe53f1277062459f4ebd91cab72085536ee1f0f02dd3bf44（数据路径不同）"
dataset_version: "scenario_a/1"
eval_fingerprint: "sha256:e8231737ac139698541ee9c796aa66d8da593860183d7616a32c31d54d40a869"
commands:
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv sync --locked --group dev"
    exit_code: 0
    result: "Resolved 156 packages; Checked 121 packages"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache make lint"
    exit_code: 0
    result: "120 files formatted; Ruff passed; mypy 76 source files passed"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache make test"
    exit_code: 0
    result: "178 passed in 147.43s（not live / not enterprise / not performance）"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv run python scripts/run_scenario_a_golden.py --output .artifacts/eval/scenario_a_v1_core.json"
    exit_code: 0
    result: "30/30 passed; five enabled metrics all 1.0; no baseline regression"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv run oria demo --output json --data-dir .artifacts/v01-core-source-20260829T101418+0800（连续两次）"
    exit_code: 0
    result: "源码态全新目录双跑通过；第二次初始化幂等，run/correlation 独立"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache make smoke"
    exit_code: 0
    result: "oria 0.1.0"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache make build"
    exit_code: 0
    result: "首次在受限沙箱因 PyPI DNS 失败；按权限流程允许网络后成功构建 wheel 和 sdist"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv pip install --python .venv/bin/python --target .artifacts/v01-core-wheel-20260829T101418+0800/site-packages --no-deps dist/oria-0.1.0-py3-none-any.whl"
    exit_code: 0
    result: "安装到隔离 site-packages"
  - cmd: "PYTHONPATH=<isolated-site-packages> .venv/bin/python scripts/verify_t08_wheel.py --work-dir .artifacts/v01-core-wheel-20260829T101418+0800/fresh"
    exit_code: 0
    result: "已安装 wheel 在无源码 cwd 双跑通过；10 商家、2 工具、零业务副作用"
artifacts:
  - "dist/oria-0.1.0-py3-none-any.whl (sha256:ac013836fdc5a78303eb9831ec2bbb78691f557e0e14246bcf5d093e288900ca)"
  - "dist/oria-0.1.0.tar.gz (sha256:f1e855827355ab6e8e1d0f01b3c39b4a8f97e8a9f1c4ac12e62d8aece97dc99f)"
  - ".artifacts/eval/scenario_a_v1_core.json"
  - ".artifacts/v01-core-source-20260829T101418+0800/reports-tmp/"
  - ".artifacts/v01-core-wheel-20260829T101418+0800/fresh/data/reports-tmp/"
evidence_refs:
  - "eval/datasets/scenario_a/v1.manifest.json (sha256:3658665125a1b259cb6026a639c032657002fc16d51b92f099cc0479064e4529)"
  - "eval/datasets/scenario_a/v1.jsonl (sha256:6f706e5307d39054cc2dd232eecefbf9099cccdcd6f42c398bf0b1363a81f325)"
  - "eval/baselines/scenario_a/1.json (sha256:76d369d18caf199eb34ba89a58a7c77f7501f66c9707b694793c5de51c176050)"
  - "eval/config/gates.yaml (sha256:8cec54ce608f55a2d7e4679cb965ff5ba3e67165d5e497df1a71075683c29cc8)"
  - "uv.lock (sha256:bc4b7cea4ab80d6c3733680ffca96027e6fa3327bea3f4137f983d3a186a6c4a)"
assertions:
  - id: "core-toolchain"
    covered: true
    note: "锁定依赖同步、Ruff/format、mypy、178 项 Core 测试、构建和 CLI smoke 均有成功退出码。"
  - id: "approved-golden-no-regression"
    covered: true
    note: "30 条已人工批准 Golden 全部通过；case/critical/outcome/tool-sequence/grounded-proposal 指标均为 1.0。"
  - id: "prompt-injection-denied"
    covered: true
    note: "sa-v1-027 与安全测试确认不受信文本不能绕过 policy/schema 或变出写 Tool；Demo 只有两个只读 Tool。"
  - id: "deterministic-merchant-eligibility"
    covered: true
    note: "sa-v1-028/029/030 与 Demo 二次校验确认黑名单、非正常营业或不合规商家不得进入推荐；结果为 10 家硬资格商家。"
  - id: "scenario-a-s1"
    covered: true
    note: "源码态和已安装 wheel 均在全新目录自动初始化 SQLite/Chroma/ObjectStore，返回两类 Tool 事件、六类规则/引用、10 家商家、预览/理由/未决项，且重复执行幂等。"
  - id: "citation-and-schema-integrity"
    covered: true
    note: "四次直接 Demo run 的 schema、semantic evidence、citation resolution、六类规则和推荐候选子集校验均为 true。"
  - id: "business-side-effect-free"
    covered: true
    note: "源码态与 wheel 态 business DB 均只有 alembic_version_business/merchants，无 Campaign/CouponBatch 表或写入。"
  - id: "installed-wheel-source-independence"
    covered: true
    note: "verifier 导入路径位于隔离 site-packages，子进程 cwd 是新建目录；wheel 内包含 migration、prompt 和 Demo 数据资源，不包含 .artifacts。"
result: "passed"
blocked_by: []
known_limits:
  - "V0.1-T10 真实 DeepSeek + BGE 必需 Live 卡未执行；V0.1 不得声称‘全部通过’。"
  - "验证使用 DemoMockLLMProvider + FixtureEmbedder；不构成真实模型质量、成本或延迟证据。"
  - "企业 Adapter 未实现、未验证。"
  - "GitHub Actions 已补充 T08 wheel Demo 门禁，但当前未提交变更尚未在远程实跑；不将本地结果写成远程 CI 通过。"
  - "当前 T07–T09 变更未提交，因此 commit 为 null，base_commit 仅用于界定工作树基线。"
---

# V0.1-T09 / V0.1-Core 验证报告

## 结论

V0.1-T09 与 `V0.1-Core` 门禁通过。T01–T09 的代码、已批准 Golden、README、初版威胁模型、Eval ADR 和证据模板已收口；场景 S1 在源码态和已安装 wheel 中都完成全新目录离线双跑，无 Campaign/CouponBatch 副作用。本结论允许以 `V0.1-Core` 作为后续任务的依赖。

V0.1-T10 真实 DeepSeek + BGE Live 卡未执行，所以版本状态是“V0.1 Core 已通过，Live 待验证”，不是“V0.1 全部通过”。

## 验证等级分离

| 卡片 | 状态 | 本报告能证明的范围 |
| --- | --- | --- |
| Fixture | passed | Mock Provider/Embedder、Golden harness、严格 schema、安全与失败边界 |
| Community | passed | 真实本地 SQLite/Chroma/ObjectStore、migration/checkpointer、源码与 wheel Demo |
| Live | not-run | 不能证明真实 DeepSeek/BGE 质量、request ID、成本或延迟 |
| Enterprise | not-run | 不能证明任何企业 Adapter 已接入 |

## 关键安全结果

- 提示词注入不能绕过系统规则、PolicyEngine、Tool schema 或只读 Tool 白名单。
- 黑名单、非正常营业和其他不合规商家由确定性资格层排除，LLM 不能将它们塞回提案。
- 所有规则和商家证据均通过快照/引用回查；最终推荐是硬资格候选集的子集。
- Demo 只产生提案预览与脱敏验证报告，没有 Campaign/CouponBatch 表、记录或外部调用。
