# V0.1-T01 验证报告

```yaml
run_id: "20260827T084858+0800"
version: "V0.1"
task_id: "V0.1-T01"
depends_on: []
verification_level: "F"
commit: "unavailable; Git 已初始化但尚无提交"
executed_at: "2026-08-27T08:48:58+08:00"
environment: "macOS 26.6.1 x86_64 / Python 3.11.15 / uv 0.12.6"
provider_model: null
config_fingerprint: "sha256:e7c3bd6f6afbb6d39421ba16ba3038d7447857155d12050c92446f99d5359ba8"
dataset_version: null
eval_fingerprint: null
commands:
  - "uv lock --check"
  - "uv sync --locked --group dev"
  - "uv run ruff format --check ."
  - "uv run ruff check ."
  - "uv run mypy src/oria"
  - "uv run pytest -m 'unit or contract'"
  - "uv build"
  - "从 dist/oria-0.1.0-py3-none-any.whl 安装到全新 Python 3.11 临时环境并运行 oria --version"
  - "uv sync --locked --group dev --extra standard"
  - "uv run --extra standard python -c 'import sentence_transformers; print(sentence_transformers.__version__)'"
artifacts:
  - "pyproject.toml"
  - "uv.lock"
  - ".github/workflows/ci.yml"
  - "dist/oria-0.1.0-py3-none-any.whl（本地构建产物，已忽略）"
evidence_refs:
  - "reports/verification/v0.1/20260827T084858+0800/summary.md"
assertions:
  - "uv 锁文件可解析，默认环境使用 Python 3.11.15"
  - "Ruff format/Lint 与 mypy strict 通过"
  - "7 个 unit/contract 选集测试全部通过"
  - "oria CLI 源码态和 wheel 安装态均输出 0.1.0"
  - "CI 包含 quality/test-core/package/extras-smoke 四个 job"
  - "Live 缺少开关、Live 未知 target、Enterprise 空 target 均在外部请求前以退出码 2 阻断"
  - "standard extra 在不下载模型的前提下成功导入 sentence-transformers 5.7.0"
result: "passed"
blocked_by: []
known_limits:
  - "未在 GitHub-hosted runner 实际执行 CI；仅完成本地等价命令和 workflow 结构检查"
  - "未实现 Agent/Graph/RAG/DB 业务能力；这些属于后续任务"
  - "standard extra 只验证 import，不代表 BGE 模型下载、推理或质量验证通过"
  - "未运行 Live Provider 或 Enterprise Adapter"
```

## 失败与修正记录

1. 首次同步解析到 `onnxruntime 1.29.0`，该版本无 Intel macOS wheel。增加
   `onnxruntime>=1.23.2,<1.24` 的 x86_64 macOS 条件约束后重新锁定，同步通过。
2. 首次 `standard` import 中 Torch 2.2.2 与 NumPy 2.x ABI 不兼容，且 Transformers 5.x
   要求更高 Torch。将 Intel macOS extra 固定为 NumPy 1.26/Torch 2.2，并限定
   Transformers 4.x，重新锁定后 `sentence-transformers 5.7.0` 导入通过。
3. 首次 CLI UT 发现 Typer 根回调不允许无子命令执行，`--version` 退出 2。
   开启 `invoke_without_command` 后回归通过。

## 真实性边界

本报告只证明 V0.1-T01 工程基线通过。未执行真实模型、真实 Embedding、
社区业务闭环或企业 Adapter 验证。
