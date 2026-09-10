---
run_id: "20260910-t06"
version: "V0.5"
task_id: "V0.5-T06"
milestone_id: "V0.5-Core"
depends_on:
  - "V0.5-T02"
  - "V0.5-T03"
  - "V0.5-T04"
verification_level: "fixture | community | security"
commit: "ab6a3de78fc130bdd6a0d0b7c3aba93d82e25464 + scoped test-format/report working tree"
worktree_state: "dirty; scoped assertions/formatting, status documentation, and this report"
executed_at: "2026-09-10T14:01:18+08:00"
environment:
  os: "macOS 26.6.1 (25G76) x86_64"
  python: "3.11.15"
  uv: "0.12.6 (7938ca5d5)"
  install_mode: "source"
provider_model:
  provider: "mock"
  model: null
  revision: null
  request_ids: []
config_fingerprint: "null; no Provider/eval configuration used"
dataset_version: "null; synthetic test fixtures only"
eval_fingerprint: "null; S2-S4 are lifecycle/security assertions, not an eval dataset run"
commands:
  - "uv run pytest tests/contract/test_v05_long_term_memory.py tests/security/test_v05_memory_boundaries.py tests/security/test_v05_abac_guardrails.py tests/security/test_v05_supervisor_boundaries.py -q"
  - "make lint"
  - "uv run pytest -m 'not live and not enterprise and not performance' -q"
  - "uv run pytest -m security -q"
artifacts:
  - "reports/verification/v0.5/20260910-t06/summary.md"
  - "docs/security/V0.5威胁模型.md"
  - "docs/security/V0.5-Memory保留与删除.md"
evidence_refs:
  - "tests/contract/test_v05_long_term_memory.py"
  - "tests/security/test_v05_memory_boundaries.py"
  - "tests/security/test_v05_abac_guardrails.py"
  - "tests/security/test_v05_supervisor_boundaries.py"
  - "tests/contract/test_v05_supervisor.py"
assertions:
  - "S2 opt-in 事实可跨 session 检索；删除后 search/view/export、向量和缓存均不可召回"
  - "S2 删除审计仅保留 object_hash 等脱敏元数据，不含正文"
  - "S3 投毒记忆为 untrusted_data/non_authoritative，不扩权"
  - "S4 三种身份的可见工具、PolicyDecision 与执行结果一致"
  - "S4 跨 tenant、伪造 role 和 RAG 诱导越权均拒绝并留审计"
result: "passed"
blocked_by: []
known_limits:
  - "No real network, Live Provider, Enterprise adapter, or performance run was executed."
  - "Live single/multi comparison remains V0.5-T07; this card does not claim multi-agent quality improvement."
  - "Community has no external backup adapter, distributed cache, checkpoint expiry job, or restore-delete replay card."
---

# V0.5-T06 S2–S4 Core 验证卡

## 1. 结论

- 实际结果：`passed`。
- 允许声明：V0.5-S2 在本地 SQLite/确定性 embedder 上的 Community Memory 生命周期通过；S3/S4 的 Security 投毒、动态最小权限和越权拒绝断言通过。V0.5 威胁模型与 Memory 保留/删除说明已落地。
- 禁止声明：本卡不证明 Live 模型表达质量、single/multi 质量提升、Enterprise 身份/数据库/DLP，也不证明 checkpoint/外部备份已立即物理清零。
- 下一门禁：V0.5-T07 必需 Live single/multi 对照；完成前只能声明多智能体控制流已实现。

## 2. 环境与身份

| 项目 | 实际值 | 取证方式 |
| --- | --- | --- |
| OS / architecture | macOS 26.6.1 (25G76) / x86_64 | `sw_vers`; `uname -m` |
| Python / uv | Python 3.11.15 / uv 0.12.6 | `uv run python --version`; `uv --version` |
| commit / worktree | `ab6a3de78fc130bdd6a0d0b7c3aba93d82e25464` + scoped test-format/report | `git rev-parse HEAD`; `git status --short` |
| `uv.lock` hash | `sha256:f1683586845697ed1095d4266a6656f32fe8f5c41e3637101975bd4716c55075` | `shasum -a 256 uv.lock` |
| 配置指纹 | null；未运行 Provider/eval | 命令范围核对 |
| 数据集 / manifest hash | null；仅合成 pytest fixture | 测试源码核对 |
| Provider / model / revision | Mock/本地组件；无真实 model/revision | 未启用 Live 开关 |
| request ID | 无 | 未发起真实网络请求 |

## 3. 实际执行命令

| # | 可复制命令 | 退出码 | 结果摘要 | 原始证据 / artifact |
| --- | --- | --- | --- | --- |
| 1 | `uv run pytest tests/contract/test_v05_long_term_memory.py tests/security/test_v05_memory_boundaries.py tests/security/test_v05_abac_guardrails.py tests/security/test_v05_supervisor_boundaries.py -q` | 1 | 首次 10 passed, 1 failed；新增 forged-role reason 文案断言与实现字符串不一致 | pytest 终端输出；未保存含原始内容的临时文件 |
| 2 | 同上 | 0 | 按实际 reason 修正后 11 passed in 5.09s | pytest 终端输出 |
| 3 | 同上 | 0 | 补删除审计 actor 断言后最终 11 passed in 4.23s | pytest 终端输出 |
| 4 | `make lint` | 2 | 首次在 Ruff format check 失败；1 个新增长条件需机械换行，324 files already formatted；Ruff check/mypy 尚未执行 | make/Ruff 终端输出 |
| 5 | `make lint` | 0 | 换行后及最终断言后均通过；最终 325 files formatted，Ruff passed，mypy 161 source files passed | make/Ruff/mypy 终端输出 |
| 6 | `uv run pytest -m "not live and not enterprise and not performance" -q` | 0 | 最终 969 passed, 1 deselected, 4 warnings in 350.65s；补 actor 断言前同样 969 passed | pytest 终端输出 |
| 7 | `uv run pytest -m security -q` | 0 | 最终 117 passed, 853 deselected in 50.01s；补 actor 断言前同样 117 passed | pytest 终端输出 |

4 条 warning 全部是既有 SQLite/Alembic migration 测试中，SQLAlchemy 通过 PRAGMA 反射 `enrollment_items` 复合外键时无法定位 SQL-parsed constraint 的 `SAWarning`。本任务未修改 migration。

## 4. 断言矩阵

| ID | 可观察断言 | 直接路径 | 绕过 / 失败路径 | 证据 | 结果 |
| --- | --- | --- | --- | --- | --- |
| S2-01 | 明确 opt-in 事实在新 session 可检索 | save → 关闭 runtime → 新 session search | `opt_in=False` 拒绝 | `test_opt_in_memory_survives_sessions_and_supports_export_delete` | passed |
| S2-02 | 删除后 search/view/export 不可召回 | 授权删除 | 已预热 search cache 后删除 | `test_delete_clears_body_vector_cache_and_writes_only_redacted_audit` 新增 view/export 断言 | passed |
| S2-03 | 正文、向量和缓存同步失效 | 主表正文清空、向量计数 0、cache 空 | 从删除前缓存再查 | 同上，直接查 SQLite 与 `_search_cache` | passed |
| S2-04 | 审计只留删除元数据，不留正文 | `memory:deleted` 事件 | payload 泄漏被删正文 | 同上，payload keys 等于 `{object_hash}`，另断言 resource ID 和 actor 字段 | passed |
| S2-05 | tenant/subject、TTL、低置信与导出生命周期 | active view/search/export | 跨 namespace、过期、低置信注入 | `test_namespace_ttl_and_confidence_filtering` 及既有 S2 契约 | passed |
| S3-01 | 投毒记忆检索为 `untrusted_data`/`non_authoritative` | `search_memory` | 文本要求忽略规则并调高风险工具 | `test_poisoned_memory_stays_untrusted_and_cannot_expand_tool_authority` | passed |
| S3-02 | 投毒内容不扩大工具权限或成为权威事实 | 既有 allowlist + PolicyDecision | `consumer:publish` 越权请求 | 同上，断言 deny reason | passed |
| S4-01 | 只读运营/活动管理员/审批人可见工具与 PolicyDecision 及执行一致 | 三角色逐工具授权与执行 | 各角色对其他工具执行 | `test_dynamic_tool_exposure_matches_read_admin_and_approver_roles` 新增 PolicyDecision/执行断言 | passed |
| S4-02 | 属性变化后执行前重新鉴权 | 暴露时 allow | 执行时 deny、工具实现未运行 | `test_tool_execution_reauthorizes_after_policy_change` | passed |
| S4-03 | 跨 tenant、伪造 role、RAG 诱导越权均拒绝并审计 | 可信 reader 的只读集合 | 三条独立 deny reason code：`cross_tenant`/`context_mismatch`/`role_denied` | `test_cross_tenant_forged_role_and_rag_instruction_cannot_expand_tools` 新增独立拒绝/审计断言 | passed |
| S4-04 | Subagent 继承但不扩大调用方权限 | spec allowlist ∩ caller policy | spec 外工具不可见，未授权工具执行拒绝 | `test_subagent_visibility_is_allowlist_intersection_and_execution_reauthorizes` | passed |

T06 没有新建测试函数；只在已有 `test_delete_clears_body_vector_cache_and_writes_only_redacted_audit`、`test_dynamic_tool_exposure_matches_read_admin_and_approver_roles` 和 `test_cross_tenant_forged_role_and_rag_instruction_cannot_expand_tools` 中补缺失断言。S2 跨 session/opt-in/TTL，S3 trust/authority/不扩权，S4 重鉴权/Subagent 交集已有覆盖，未重复添加。

## 5. 验证等级分离

| 卡片 | 目标 | 本次状态 | 证据边界 |
| --- | --- | --- | --- |
| Fixture | 确定性 embedder/Tool/Principal/投毒文本 | passed | 仅合成 fixture 与 Mock Provider |
| Community | 本地 SQLite Memory 主表、向量投影、缓存和审计 | passed | 单进程本地数据目录，无外部备份/分布式缓存 |
| Security | Memory 投毒、ABAC、动态最小权限、伪造/越权与审计 | passed | 本地策略与合成身份 |
| Live | 真实 Provider/模型表达与 single/multi 对照 | not-run | V0.5-T07 |
| Enterprise / E-like | 企业身份、数据库、DLP、备份恢复 | not-run | 留 V0.6/企业栈 |

## 6. 安全、数据与副作用

- 信任边界与本次威胁 ID：见 `docs/security/V0.5威胁模型.md` 的 V05-TM-01–V05-TM-13。
- 数据类型：合成用户偏好、合成投毒文本、本地角色/tenant fixture；无真实客户数据。
- tenant/actor/executor：由 `Runtime.new_context` 和 `LocalPolicyEngine` 可信主体目录映射；伪造主体与跨 tenant 资源 fail closed。
- RAG/Memory 注入：仅作不受信数据；检测命中告警不参与授权，越权工具在执行前拒绝。
- 预期副作用：S2 创建并删除本地合成 Memory，写入脱敏删除审计；无业务系统、远程或生产写入。
- 脱敏检查：报告不保存提示词原文、凭证或 PII；删除审计 payload 键精确断言为 `object_hash`。

## 7. Artifacts 与可复现性

- `reports/verification/v0.5/20260910-t06/summary.md`：本验证卡。
- `docs/security/V0.5威胁模型.md`：Memory、多智能体、Guardrail、ABAC 威胁与证据对应。
- `docs/security/V0.5-Memory保留与删除.md`：opt-in、TTL、删除传播、审计、checkpoint/备份边界。

本次没有数据集、Provider 或评测指纹。测试由锁定 `uv.lock` 的 Python 3.11/uv 0.12.6 源码环境重现。

## 8. 已知限制与后续

- 未执行：真实网络、Live、Enterprise、Performance、外部备份恢复、分布式缓存失效。
- 失败项：最终声明范围无；首次断言文案与首次 Ruff format 失败已如实保留并修正。
- `blocked_by`：无。
- 已接受剩余风险：确定性注入/输出规则可能误报或漏报；Community 无自动 checkpoint 清理和备份删除重放，不得用于作出生产物理删除 SLA 承诺。
- 后续：V0.5-T07 完成 Live single/multi 对照；V0.6/企业接入前固化有限 checkpoint/备份 retention、到期清理、删除墓碑与恢复后重放验证。

## 9. 结果判定

S2–S4 所有 Core 必需断言均有实际 Fixture/Community/Security 证据且通过，因此本卡判定为 `passed`。该结论不改变 Live 与 Enterprise 为 `not-run`的状态。
