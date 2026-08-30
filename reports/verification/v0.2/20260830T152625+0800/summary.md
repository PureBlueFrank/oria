---
run_id: "20260830T152625+0800"
version: "V0.2"
task_id: "V0.2-T05"
depends_on: ["V0.2-T03", "V0.2-T04"]
verification_level: "F；C"
base_commit: "3333000b8430d8e39ef3e386e6ba61e1d748464e"
commit: null
worktree_state: "dirty；仅含当前 V0.2-T05 变更"
executed_at: "2026-08-30T15:26:25+08:00"
environment: "macOS x86_64 / Python 3.11.15 / uv 0.12.6"
provider_model: null
embedding_model: "BAAI/bge-small-zh-v1.5@a7ec18349c42fc774b0e86af26215e38a10fbe9d"
reranker_model: "BAAI/bge-reranker-base@2cfc18c9415c912f9d8155881c133215df768a70"
config_fingerprint: "sha256:c1913fb5200d2a4c4dc205f98d85dc083c21b1c56d98e15321c892383d21016c"
dataset_version: "rag/1 approved+holdout_frozen"
eval_fingerprint: "sha256:4e1a3460af2d5d2264160653c4d7ea9dee5b5626112934278a5f1c31f689436c"
commands:
  - cmd: ".venv/bin/python scripts/run_rag_golden.py"
    exit_code: 0
  - cmd: "HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/oria eval run --suite rag --verification community --split all --gates eval/config/rag-gates.yaml --lock uv.lock ..."
    exit_code: 0
  - cmd: ".venv/bin/pytest -q"
    exit_code: 0
  - cmd: ".venv/bin/ruff format --check . && .venv/bin/ruff check . && .venv/bin/mypy src/oria"
    exit_code: 0
  - cmd: "uv build --offline"
    exit_code: 0
result: "passed"
blocked_by: []
known_limits:
  - "本轮未调用任何 LLM Provider；V0.2-T06 DeepSeek 必需 Live 卡待执行"
  - "Nightly 当前只接入零请求预检；真实 Provider 请求循环留 T06"
  - "本地 Codex 进程未继承 DEEPSEEK_API_KEY，Nightly 实测以 request_count=0 blocked；已用契约测试验证 ready 分支"
  - "当前语料是单文档六类合成规则，结果不外推到生产文档"
---

# V0.2-T05 完成验证报告

## 结论

V0.2-T05 的 Fixture 与 Community 门禁通过。RAG v1 的 60 条合成 case 已由 FrankLee 于 `2026-08-30T12:02:57+08:00` 批准，holdout 已冻结；首次确定性 baseline/gates、PR `eval-golden`、Nightly 零请求预检和真实 BGE/cross-encoder 三管线对照均已完成。

## 身份与门禁

- Dataset SHA-256：`295e440cc0a9b32368f492a22bb6ec95396f42f5b9b2c8577becec1beb94d537`。
- Gate SHA-256：`bf81649871389a72f703bafe6a9b692222297733315bb445c2ea7855a894ec33`。
- Baseline SHA-256：`c077bebbdb57ef2eaace5b09edb4267ac5a0dd067011e10b75a65801e45a7a1a`。
- `uv.lock` SHA-256：`b45a0bb6cef787031c371e3351d8bbad6b7b325388eb838c6265dc46ec5ad425`。
- Fixture baseline fingerprint：`sha256:7d8b7357bcdfca1213e72195aca968fa137e46e17cbaaebdecb366627ab95278`。
- Community fingerprint：`sha256:4e1a3460af2d5d2264160653c4d7ea9dee5b5626112934278a5f1c31f689436c`。

`eval_fingerprint` 已绑定 dataset、embedding/reranker profile、runner/pipeline、split、gate 与 dependency lock。Fixture baseline 不冻结延迟，只对确定性逐例结果、Recall@K、MRR、引用和 critical case 建立回归门禁。

## Community 原始结果

| Pipeline | Recall@3 | MRR | 引用命中率 | Critical pass | P50 | P95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| dense | 0.9333 | 0.8389 | 1.0000 | 1.0000 | 34.9 ms | 41.2 ms |
| hybrid | 0.9667 | 0.8778 | 1.0000 | 1.0000 | 85.0 ms | 100.1 ms |
| hybrid + real reranker | 0.9833 | 0.9222 | 1.0000 | 1.0000 | 1604.9 ms | 1763.0 ms |

正式运行在模型首次下载后强制 `HF_HUB_OFFLINE=1` 和 `TRANSFORMERS_OFFLINE=1` 复跑，两个模型均使用配置中的固定 40 位 revision，`trust_remote_code=false`。真实 reranker 在本数据集上提高了 Recall@3 和 MRR，同时明显增加 CPU 延迟；延迟只作原始观测，不作质量代理或 PR 阈值。

## 自动化结果

| 项目 | 结果 |
| --- | --- |
| 完整默认测试 | `310 passed, 1 deselected` |
| Ruff format / lint / mypy | 通过 |
| Fixture RAG Golden 复跑 | 通过；60 条 × 3 pipeline |
| Nightly missing-key blocked | 通过；`request_count=0` |
| wheel / sdist 构建 | 通过 |
| 安装态 wheel | 通过；dataset / manifest / baseline / gates / config / pricing 可读 |

## 后续

V0.2 Core 已满足进入 V0.3 的条件。V0.2-T06 仍需用显式 target 执行 DeepSeek 必需 Live 卡，并把 Nightly 从零请求预检扩展为使用预留账本的真实 Provider 请求循环。
