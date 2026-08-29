# V0.2-T04 验证报告

- run_id: 20260829T221509+0800
- version: V0.2
- task_id: V0.2-T04
- depends_on: [V0.2-T03]
- verification_level: F / C（Fixture + Community）
- executed_at: 2026-08-29T22:15:09+08:00
- environment: macOS (darwin x86_64) / Python 3.11 / uv 0.12.6

## 交付摘要

自建纯 Python BM25 检索投影（无 numpy 依赖）、dense+BM25 的 RRF 融合、`Reranker` seam + 确定性 `FixtureReranker`、`ConfigurableRetriever` 三配置（dense / hybrid / hybrid_rerank）同一接口、无静默 fallback，runtime 默认装配 dense 管线。

## 验证命令与真实输出

- `uv run pytest -m "not live and not enterprise and not performance" -q` → `282 passed, 1 deselected`
- `uv run pytest -m security -q` → `49 passed, 229 deselected`
- `uv run ruff check .` → All checks passed
- `uv run ruff format --check .` → 已格式化
- `uv run mypy src/oria` → Success: no issues found in 83 source files

## 关键断言

- BM25 评分：tf 饱和 + 文档长度归一化（`test_bm25_saturates_tf_and_normalizes_document_length`）
- BM25 ACL/tenant：未构建报错 + ACL 过滤（`test_bm25_requires_tenant_build_and_applies_acl_before_scoring`）
- 三配置同接口 + RRF 融合 + 无静默 fallback（`test_v02_retrieval_pipeline.py`）
- 三配置真实运行 + 原始延迟基线（`test_v02_retrieval_pipeline_it.py`）
- 生命周期同步：更新/删除传播到 BM25 投影（`test_v02_rag_lifecycle.py` 扩展）

## 未做 / 受限项

- 未接入真实 cross-encoder reranker（`FixtureReranker` 仅用于契约/集成测试）
- 未运行真实 BGE 三管线 Recall@K/MRR 对照（属 V0.2-T05）
- 未设性能阈值（仅记录原始延迟基线）
- 未运行 Live / Enterprise

## result

passed（Community Core；真实 reranker 与评测留 T05）
