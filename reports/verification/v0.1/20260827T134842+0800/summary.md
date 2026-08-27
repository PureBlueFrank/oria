---
run_id: "20260827T134842+0800"
version: "V0.1"
task_id: "V0.1-T02-remediation-package-gate"
depends_on: ["V0.1-T01", "V0.1-T02", "V0.1-T02-remediation"]
verification_level: "F"
commit: "246996722a9778976c85c47dd5e9816c296a44a6"
executed_at: "2026-08-27T13:48:42+08:00"
environment: "macOS 26.6.1 (Darwin 25.6.0 x86_64) / Python 3.11.15 / uv 0.12.6 / uv.lock sha256:14df51cee897e58e68b5608e245bd1a7… / pydantic 2.13.4 / pytest 9.1.1 / mypy 1.20.2 / ruff 0.16.4；宿主环境具备网络与锁定构建后端 hatchling"
provider_model: null
config_fingerprint: "sha256:c4604efae52f0027d1d345af68eaa903242fc8c7fe4169fb4469967f3dcff48f"
dataset_version: null
eval_fingerprint: null
commands:
  - cmd: "uv build"
    result: "Successfully built dist/oria-0.1.0.tar.gz (228192 B) 与 dist/oria-0.1.0-py3-none-any.whl (22967 B)"
  - cmd: "uv venv .artifacts/package-venv --python 3.11 && uv pip install --python .artifacts/package-venv/bin/python dist/oria-0.1.0-py3-none-any.whl"
    result: "隔离环境仅安装新构建的 wheel，成功"
  - cmd: ".artifacts/package-venv/bin/python scripts/verify_t02_wheel.py"
    result: "imported 20 Oria modules from .artifacts/package-venv/lib/python3.11/site-packages/oria/__init__.py（路径证明来自已安装 wheel，非源码 checkout）"
  - cmd: "uv run mypy --strict --python-executable .artifacts/package-venv/bin/python scripts/wheel_type_consumer.py"
    result: "Success: no issues found in 1 source file（下游 strict mypy 经隔离解释器消费 py.typed 与 JsonValue）"
  - cmd: ".artifacts/package-venv/bin/oria --version"
    result: "0.1.0"
  - cmd: ".artifacts/package-venv/bin/oria config doctor --output json --data-dir ./d"
    result: "退出码 0，输出脱敏 JSON（在非源码工作目录执行，证明不依赖源码 cwd）"
  - cmd: "uv run python scripts/verify_t02_remediation.py"
    result: "P0-1/P0-2/P0-3 rejected；P1-2 rejected；P1-3 rejected 且有限浮点往返 accepted；P1-4 reasoning 与 raw response 默认 redacted 且内部访问 retained；P1-5 runtime import accepted"
  - cmd: "make lint"
    result: "34 files already formatted；Ruff All checks passed；mypy Success: no issues found in 21 source files"
  - cmd: "make test"
    result: "47 passed"
  - cmd: "uv run pytest -m 'unit or contract'"
    result: "38 passed, 9 deselected"
  - cmd: "uv run pytest -m security"
    result: "9 passed, 38 deselected"
  - cmd: "ORIA_EDITION=production uv run pytest tests/security/test_runtime_boundaries.py"
    result: "4 passed（证明测试已对 ambient ORIA_* 免疫）"
  - cmd: "调用方独立复现脚本（不依赖 scripts/，另行编写）"
    result: "13 条绕道路径全部被拒：P0 五条抛 RuntimeSealedError；args/attributes/content 抛 TypeError/AttributeError；NaN 与 ±Inf 抛 ValidationError；reasoning 与 raw_response 在 repr 与 model_dump_json 均不出现；正向路径 1.5 浮点往返与 aclose 正常"
artifacts:
  - "dist/oria-0.1.0-py3-none-any.whl、dist/oria-0.1.0.tar.gz（本 run 重新构建）"
evidence_refs:
  - "reports/verification/v0.1/20260827T084858+0800/summary.md（T01 工程基线）"
  - "reports/verification/v0.1/20260827T112706+0800/summary.md（T02 首次报告；result: passed 判定过早，原文件保留不改）"
  - "reports/verification/v0.1/20260827T133710+0800/summary.md（T02 remediation；result: blocked，本 run 为其 package 门禁补验）"
assertions:
  - id: "package 门禁 1 / uv build"
    covered: true
    note: "上一 run 的 blocked 原因是受限执行环境无网络且本地无 hatchling，属环境限制而非代码或 pyproject 缺陷。本 run 在具备网络的宿主环境删除旧 dist 后重新构建成功。"
  - id: "package 门禁 2 / 已安装 wheel 全模块 import"
    covered: true
    note: "以上一 run 提供的 scripts/verify_t02_wheel.py（pkgutil.walk_packages 遍历，非手写清单）在仅装 wheel 的隔离 venv 中导入 20 个模块成功，输出路径位于 site-packages。同时验证 wheel 内 CLI 在非源码 cwd 下可运行。"
  - id: "package 门禁 3 / 下游 py.typed 与 JsonValue"
    covered: true
    note: "以隔离解释器为 --python-executable 执行 strict mypy 通过。这一项是 T02 首次修复的真实缺口：当时仅用 pydantic.JsonValue 重导出，运行时可用但下游 mypy 报 does not explicitly export attribute；本 run 确认显式导出后已消除。"
  - id: "P0/P1 修复有效性（调用方独立复核）"
    covered: true
    note: "调用方未使用 scripts/ 内脚本，另行编写复现脚本验证 13 条绕道路径，全部被明确拒绝且正向路径保持可用，与上一 run 自述一致。"
  - id: "V01-LIFE-02 / V01-CTX-01 / V01-LOG-01 / V01-CFG-04 重新裁定"
    covered: true
    note: "首次报告中这四条判为 covered: true 是错误的：它们只验证了直接路径被拒，未验证替换整个 stack、经进程级 runtime 传递元数据、model_dump_json 序列化、嵌套容器原地修改这四类绕道。remediation 后绕道均被封堵并有回归测试锁定，本 run 确认。"
  - id: "CI required 覆盖"
    covered: true
    note: "test-core 由 'unit or contract' 改为 'not live and not enterprise and not performance'，9 个 security 测试纳入必需检查；package job 增加 wheel 全模块 import 与下游 strict mypy。四个独立 required job 拆分语义保持不变。本 run 后需由实际 CI 运行确认。"
result: "passed"
blocked_by: []
known_limits:
  - "验证等级仅为 F（Fixture）。未调用任何真实 Provider、未下载或推理真实 Embedding 模型、未接入真实数据库或企业系统。不构成 V0.1 Core Gate、Live 卡或 Enterprise 卡的证据。"
  - "本 run 及上一 run 均未实现 T03/T04：无 migration、Repository/EligibilityPolicy、RAG/Retriever、Tool、StateGraph/Checkpoint、data init、demo。"
  - "V0.1 Core Gate 仍未通过（尚缺 T03–T09）；Live 卡仍未运行。T02 至此可标为『remediation 后完成』，不等于 V0.1 通过。"
  - "上一 run 的 result: blocked 未被改写。按路线 §1.1 第 5 条，失败与阻塞历史保留，本 run 以新 run 追加补验结果。"
  - "本 run 的 package 门禁在宿主 macOS 环境执行。GitHub Actions 上的 package job 首次运行结果需另行确认；Linux 与 macOS 的 wheel 构建/类型消费差异未在本 run 覆盖。"
  - "participant 事实：T02 骨架源码由 Codex 产出；首批测试由 OpenCode/glm-5.3 补齐；JsonValue 首次（不完整）修复与首份报告由 Hermes 完成；缺陷审查与 remediation 由 Codex 完成；本 run 的 package 门禁补验与独立复核由 Hermes 完成。"
  - "IM channel 各真实 adapter 的凭证必填矩阵仍有意延后到对应 Notifier/Ingress 首次落地时收紧，理由见上一 run 报告。"
  - "T01 报告中记录了当时尚不存在的 ResolvedRuntimeConfig fingerprint，来源与复现方式仍存疑。按真实性规则未改写 T01 旧报告，建议后续做一次证据审计。"
---

# V0.1-T02 remediation：package 门禁补验报告

## 结论

上一 run `20260827T133710+0800` 因受限环境无法取得锁定构建后端 `hatchling` 而记为 `blocked`，其 `blocked_by` 明确指向环境而非代码。本 run 在具备网络的宿主环境按该报告「下一步门禁」列出的三步逐条执行，全部通过：

1. `uv build` 重新构建 wheel 与 sdist
2. 仅安装新 wheel 的隔离 venv 中导入 20 个模块成功
3. 以隔离解释器为 `--python-executable` 的下游 strict mypy 通过

因此 `V0.1-T02` 至此判定为 **remediation 后完成**，可并行进入 `V0.1-T03` 与 `V0.1-T04`。**V0.1 Core Gate 仍未通过，Live 卡仍未运行。**

## 本轮修正的一项历史误判

首次报告 `20260827T112706+0800` 将 V01-LIFE-02、V01-CTX-01、V01-LOG-01、V01-CFG-04 判为 `covered: true`，并给出 `result: passed`。该判定错误，原因是这些断言只验证「直接路径被拒绝」，没有验证「绕道」：

| 用例 | 当时验证了什么 | 当时漏掉的绕道 |
| --- | --- | --- |
| V01-LIFE-02 | 直接 `enter_async_context` 抛 `LifecycleSealedError` | 整体替换 `_exit_stack` 即可绕过，且原 stack 永不关闭 |
| V01-CTX-01 | 两个 Context 各自读到自身身份 | 往进程级 `RuntimeServices` 写属性即可让 B 看到 A 的 `run_id` |
| V01-LOG-01 | 敏感字段不在 `repr()` | `repr=False` 不阻止序列化，`model_dump_json()` 仍输出思维链与 provider 原始响应 |
| V01-CFG-04 | 顶层字段赋值抛 `ValidationError` | `frozen=True` 仅浅冻结，`args`/`attributes`/`content` 可原地篡改 |

其中 `dir(ctx)` 结构性断言不仅无效，还掩盖了上述 CTX-01 的真实泄漏。这四条现已由 remediation 封堵并有回归测试锁定。旧报告按真实性规则保留不改。

## 一个值得记录的方法论差异

首次实现与首批测试关注「设计意图是否被实现」，而缺陷审查关注「约束能否被绕过」。`JsonValue` 的 `RecursionError` 属同一模式的另一面：静态检查验证「代码是否符合类型」，而没有人验证「import 是否真能执行」。T03 起的每个任务，建议在测试清单中显式区分这两类断言。

## 测试规模变化

| 阶段 | 测试数 |
| --- | --- |
| T01 基线 | 7 |
| T02 首次 | 24 |
| T02 remediation 后 | 47 |
