---
run_id: "20260827T133710+0800"
version: "V0.1"
task_id: "V0.1-T02-remediation"
depends_on: ["V0.1-T01", "V0.1-T02-initial-review"]
verification_level: "F"
commit: "339c9c2e50387a35bfc4cbb144ef5e70a7e6e5de (working tree contains uncommitted remediation; no commit created by instruction)"
executed_at: "2026-08-27T13:37:10+08:00"
environment: "macOS 26.6.1 (Darwin 25G76 x86_64) / Python 3.11.15 / uv 0.12.6 / uv.lock sha256:14df51cee897e58e68b5608e245bd1a761131acc30dba0ae50a8eb19f1ef17ae / pydantic 2.13.4 / pytest 9.1.1 / mypy 1.20.2"
provider_model: null
config_fingerprint: "sha256:5aaba68153bb9950eaec218442a25a97b7489c3a09e830f9eeabf2d1b717a391"
dataset_version: null
eval_fingerprint: null
commands:
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache make lint"
    result: "34 files formatted; Ruff format/check passed; mypy: Success, 21 source files"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache make test"
    result: "47 passed"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv run pytest -m 'unit or contract'"
    result: "38 passed, 9 deselected"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv run pytest -m security"
    result: "9 passed, 38 deselected"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv run pytest -m 'not live and not enterprise and not performance'"
    result: "47 passed"
  - cmd: "ORIA_EDITION=production UV_CACHE_DIR=.artifacts/uv-cache uv run pytest tests/security/test_runtime_boundaries.py::test_outputs_and_serializations_do_not_leak_secrets"
    result: "1 passed；证明普通测试不受 ambient ORIA_* 污染"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv build"
    result: "failed：仓库内 cache 无 hatchling，受限环境 DNS/网络不可用，3 次 fetch 后退出非零"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv build --offline"
    result: "failed：hatchling>=1.27,<2 不在仓库内 cache，退出非零"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv build --no-build-isolation"
    result: "failed：仓库本地 .venv 未安装 hatchling，ModuleNotFoundError，退出非零"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv run mypy --strict scripts/wheel_type_consumer.py"
    result: "Success: no issues found in 1 source file；仅源码态诊断，不能替代已安装 wheel 门禁"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv run python scripts/verify_t02_wheel.py"
    result: "源码态导入 20 个 Oria 模块；不能替代已安装 wheel 门禁"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv run python scripts/verify_t02_remediation.py"
    result: "P0/P1 八组复现路径全部由可利用缺陷变为明确拒绝或默认脱敏；有限浮点和内部显式访问正向路径通过"
artifacts:
  - "docs/adr/ADR-030-deep-immutable-seam-values.md"
  - "src/oria/_internal/immutable.py"
  - "src/oria/core/{context,types}.py"
  - "src/oria/config/{models,resolve}.py"
  - "tests/contract/{test_core_types,test_imports_and_schemas,test_runtime_lifecycle,test_context_isolation,test_config_resolution}.py"
  - "tests/security/{test_runtime_boundaries,test_config_boundaries}.py"
  - "tests/conftest.py"
  - "scripts/{verify_t02_remediation,verify_t02_wheel,wheel_type_consumer}.py"
  - ".github/workflows/ci.yml"
evidence_refs:
  - "reports/verification/v0.1/20260827T084858+0800/summary.md（T01 工程基线）"
  - "reports/verification/v0.1/20260827T112706+0800/summary.md（T02 首次报告；result: passed 判定过早，原文件保留不改）"
assertions:
  - id: "P0-1 / V01-CTX-01"
    covered: true
    note: "RuntimeServices 使用 slots + 构造完成后强制封存；actor/run_id/session_id/stashed_run_id 等新增执行元数据抛 RuntimeSealedError，两个 Context 的 run/tenant/actor 行为保持隔离。"
  - id: "P0-2 / V01-LIFE-02"
    covered: true
    note: "config/policy/domain、六个 registry、全部可选资源和 _exit_stack 均不可替换/删除；替换 tools 与原 registry 的 late register 分别明确失败。"
  - id: "P0-3 / V01-LIFE-01"
    covered: true
    note: "替换 _exit_stack 明确失败，aclose 关闭原 stack；另补 __aenter__ 抛错时仅逆序 unwind 已进入资源。"
  - id: "P1-2 / ADR-030"
    covered: true
    note: "ValueModel 校验后递归复制并冻结 mapping/list/tuple/set；args、attributes、Message.content 及嵌套容器原地修改失败，修改原输入容器不影响模型。"
  - id: "P1-3"
    covered: true
    note: "所有 ValueModel 递归拒绝 NaN/Infinity/-Infinity；有限 1.25/12.375 经 JSON round-trip 保持。"
  - id: "P1-4 / V01-LOG-01"
    covered: true
    note: "ReasoningDelta.text 与 ChatResult.raw_response 使用 repr=False + exclude=True；默认 model_dump/model_dump_json 不输出，internal_text()/internal_raw_response() 明确保留内部访问。"
  - id: "P1-5"
    covered: "partial"
    note: "pydantic.JsonValue 以显式同名导入和 __all__ 导出；源码态 strict mypy 通过。新 wheel 无法构建，因此已安装 wheel 的下游 mypy 尚未执行。"
  - id: "P1 config matrix"
    covered: true
    note: "provider/api_dialect、structured_output/modes 交叉约束生效；log level、三类 telemetry exporter、im.default 均收紧枚举。ResolvedIMConfig 保留深度只读 channel 配置，公开摘要不输出凭证。"
  - id: "P1-7 / CI coverage"
    covered: true
    note: "test-core 改为 not live and not enterprise and not performance，覆盖 security 与无外部依赖 integration；package job新增 wheel 全模块 import 与下游 mypy 命令，仍保持四个独立 required job。"
  - id: "P3-17"
    covered: true
    note: "SealedAsyncExitStack 覆盖未 seal 已 close 后拒绝资源、重复 aclose、seal 幂等。"
result: "blocked"
blocked_by:
  - "受限执行环境无网络，仓库内 .artifacts/uv-cache 没有 hatchling；仓库本地 .venv 也未安装 hatchling。因此 uv build 无法解析/加载已在 pyproject.toml 正确声明的 build-system backend。"
  - "新 wheel 未生成，无法执行已安装 wheel 的全模块 import 与下游 py.typed/mypy 消费门禁；旧 dist 产物早于 remediation，未被拿来冒充新证据。"
known_limits:
  - "验证等级仅为 F；未调用真实 Provider、Embedding、数据库或企业系统，不构成 V0.1 Core/Live/Enterprise 证据。"
  - "本轮严格未实现 T03/T04：无 migration、Repository/EligibilityPolicy、RAG、Tool、StateGraph、data init 或 demo。"
  - "上一份报告 20260827T112706+0800 的 result: passed 判定过早；本次为其 remediation。旧报告按真实性规则保留不改，本次因 package 门禁未完成仍不写 passed。"
  - "参与者事实：T02 骨架源码由 Codex 产出；测试由 OpenCode/glm-5.3 补齐；JsonValue 首次修复与提交由 Hermes 完成；本次审查与 remediation 由 Codex 完成。"
  - "IM channel 结构、选中 channel 存在性与枚举已校验；每种真实 channel 的凭证组合完整性有意延后到对应 Notifier/Ingress adapter 首次落地时收紧，因为 webhook 与 app_id/app_secret 的必填矩阵不同，T02 尚无真实消费者。"
  - "T01 报告记录了当时尚不存在的 ResolvedRuntimeConfig fingerprint，其生成来源与复现方式仍存疑；按要求不改写 T01 旧报告，建议后续证据审计单独核对。"
---

# V0.1-T02 remediation 验证报告

## 结论

本 run 修复了调用方独立复现的 P0/P1 缺陷，源码静态检查、47 个本地测试、security marker、环境隔离和八组直接复现脚本均通过。上一份 `20260827T112706+0800` 报告在 Runtime 可替换、嵌套值可变、敏感字段默认可序列化且 required CI 漏跑 security 的情况下写成 `result: passed`，判定过早；该历史文件保持不动，本报告追加纠正。

本 run 最终为 **blocked**，不是 passed：受限环境无法下载且本地没有 `hatchling`，所以新 wheel 未生成，已安装 wheel 的 import + 下游 mypy 门禁没有执行。按路线“任务完成”定义，在这个 package 门禁补齐前不重新关闭 T02，也不进入 T03/T04 主线实现。

## 修复摘要

- Runtime 使用一次性构造、完成即封存的状态；所有组成、私有 exit stack 与未知执行元数据的赋值/删除均明确失败，`aclose()` 仍可重复安全调用。
- ADR-030 选择校验时深度冻结。拒绝“各 seam 自行复制”和“只在传递前约定规范化”，因为它们无法保证同一个已校验对象在授权、哈希或 checkpoint 前保持稳定。
- 非有限浮点在值模型校验期递归拒绝；Provider capability 与配置组合增加交叉校验。
- reasoning 和 provider raw payload 保留内部显式访问，但默认 repr/字典/JSON 投影全部排除。
- `ResolvedIMConfig` 不再丢弃 channels；公开摘要仍只暴露安全投影。真实 adapter 各自的凭证必填组合按上方 known limit 延后。
- CI 的 `test-core` 收集全部无外部依赖测试；`package` 增加新 wheel 的全模块 import 和下游 strict mypy，但本机因构建后端不可用未能实跑该部分。

## 下一步门禁

在具备锁定构建依赖的环境执行以下三步，并把真实输出追加到新 run（失败历史继续保留）：

1. `UV_CACHE_DIR=.artifacts/uv-cache uv build`
2. 在 `.artifacts/package-venv` 只安装新 wheel，执行 `scripts/verify_t02_wheel.py`
3. 用该隔离解释器作为 `--python-executable` 执行 `mypy --strict scripts/wheel_type_consumer.py`

三项通过后，T02 才可标为“remediation 后完成”，并可并行开始 T03/T04。
