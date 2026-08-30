---
run_id: "20260830T104055+0800"
version: "V0.2"
task_id: "V0.2-T05"
depends_on: ["V0.2-T03", "V0.2-T04"]
verification_level: "F；C 待执行"
base_commit: "3333000b8430d8e39ef3e386e6ba61e1d748464e"
commit: null
worktree_state: "dirty；仅含当前 V0.2-T05 进行中变更"
executed_at: "2026-08-30T10:40:55+08:00"
updated_at: "2026-08-30T11:38:54+08:00"
environment: "macOS x86_64 / Python 3.11.15 / uv 0.12.6"
provider_model: null
embedding_model:
  fixture: "FixtureEmbedder"
  community_planned: "BAAI/bge-small-zh-v1.5@a7ec18349c42fc774b0e86af26215e38a10fbe9d"
reranker_model:
  fixture: "FixtureReranker"
  community_planned: "BAAI/bge-reranker-base@2cfc18c9415c912f9d8155881c133215df768a70"
config_fingerprint: null
dataset_version: "rag/1 pending_human_review"
eval_fingerprint: "正式值未生成；dataset 尚未批准"
commands:
  - cmd: "uv run ruff format --check . && uv run ruff check . && uv run mypy src/oria"
    exit_code: 0
  - cmd: "uv run pytest -m 'not live and not enterprise and not performance' -q"
    exit_code: 0
  - cmd: "uv build"
    exit_code: 0
  - cmd: "uv run oria eval run --suite rag ..."
    exit_code: 2
result: "blocked"
blocked_by:
  - "60 条 RAG v1 草案尚未由用户逐条人工审阅"
known_limits:
  - "未创建 committed baseline/gates，未接入正式 eval-golden 与 eval-nightly workflow"
  - "未运行冻结 holdout、真实 BGE 或真实 cross-encoder"
  - "未调用任何公开 LLM Provider"
---

# V0.2-T05 进行中验证报告

## 结论

T05 的代码骨架、数据草案与 Fixture development 验证已通过，但任务整体按门禁记为 `blocked`：数据集仍为 `pending_human_review`。本报告不把开发集结果写成 baseline，也不声明真实 BGE/reranker 质量已验证。

## 已完成产物

- RAG v1：60 条全合成查询，42 development、18 holdout，六类规则各 10 条；12 条 critical 按两个 split 各 6 条分布；manifest 绑定 SHA-256、来源、许可和审阅状态。
- Harness：dense / hybrid / hybrid_rerank 三管线统一输出 Recall@K、MRR、引用命中率、关键用例通过率、P50/P95 延迟和 Wilson 95% 区间。
- CLI：`oria eval run --suite rag`；未审阅数据在模型加载、检索或 Provider 请求前退出 2。
- 真实 reranker adapter：固定 model/revision，`trust_remote_code=false`，非有限或数量不匹配 score 拒绝。
- Nightly 前置控制：target、价格快照有效期、凭证、数据版本和五项正预算 fail closed；预算账本请求前预留、响应后按 usage 结算，不完整 run 不能 complete。
- 新增独立 `eval` optional extra 与 CI import smoke；RAG 威胁模型已记录标签污染、ACL、版本残留、holdout 污染和成本风险。
- 人工预审 remediation：删除跨类别或源文档不可回答概念，空 critical split 改为 fail closed，检索标签仅在当前规则实例真实配置 `discount_rate` 时才加入“折扣率”。

## 实际验证

| 项目 | 结果 |
| --- | --- |
| Ruff format / lint / mypy | 通过 |
| Community 默认测试 | `301 passed, 1 deselected` |
| T05 定向测试 | `18 passed` |
| Development 三管线 Fixture run | 42 条 × 3 管线运行，引用命中率均为 1.0 |
| wheel / sdist 构建 | 通过 |
| 安装态 wheel 资源校验 | 通过；内置 dataset / manifest / config / pricing 可读 |
| eval extra import smoke | `sentence-transformers 5.7.0 / CrossEncoder` 导入通过 |
| 正式 CLI | 按预期以 `pending actual human review` 退出 2 |

Development 调试值不构成正式质量证据：dense Recall@3/MRR 为 `0.9762/0.7778`，hybrid 为 `0.9762/0.8849`，hybrid+FixtureReranker 为 `0.9524/0.8690`；三管线引用命中率和 development critical pass rate 均为 `1.0`。未读取 holdout 结果进行调参。

## 数据与模型来源

- Dataset SHA-256：`8fc68ae9fe3569b00cf5895b2dcfe7d5573328c1fa2306215a76c60a1a8bc5e4`。
- `uv.lock` SHA-256：`b45a0bb6cef787031c371e3351d8bbad6b7b325388eb838c6265dc46ec5ad425`。
- BGE 与 reranker 均使用 Hugging Face 上 BAAI 模型的固定 40 位 revision；两者声明 MIT，尚未执行本轮真实加载。
- DeepSeek 价格快照按 2026-08-30 官方页面核验，并保守使用 peak 输入/输出费率；有效期到 2026-09-30，过期后请求前阻断。

## 下一门禁

用户需要逐条确认 `eval/datasets/rag/v1.jsonl` 与 `REVIEW.md`。批准后才能更新 review metadata/hash、冻结 holdout、创建首次 baseline/gates、接入正式 CI/Nightly workflow，并执行真实 BGE + cross-encoder 三管线 C 级对照。
