# Oria 验证证据模板

> 本文是模板，不是已通过报告。复制后必须把所有占位符替换为实际值；未执行、失败或缺环境的项不得写为 passed。

```yaml
run_id: "<YYYYMMDDThhmmss+timezone>"
version: "<V0.x>"
task_id: "<V0.x-Tyy>"
milestone_id: "<V0.x-Core | null>"
depends_on:
  - "<task-or-milestone>"
verification_level: "<fixture | community | live | enterprise | e-like>"
commit: "<full-commit-sha>"
worktree_state: "<clean | dirty; list scoped changes>"
executed_at: "<ISO-8601>"
environment:
  os: "<name/version/arch>"
  python: "<version>"
  uv: "<version>"
  install_mode: "<source | installed-wheel | both>"
provider_model:
  provider: "<mock | provider-name | null>"
  model: "<exact-model-or-null>"
  revision: "<exact-revision-or-null>"
  request_ids: []
config_fingerprint: "<sha256-or-null-with-reason>"
dataset_version: "<version-or-null>"
eval_fingerprint: "<sha256-or-null-with-reason>"
commands: []
artifacts: []
evidence_refs: []
assertions: []
result: "<passed | failed | blocked>"
blocked_by: []
known_limits: []
```

## 1. 结论

- 实际结果：`passed | failed | blocked`
- 允许声明：写出本次证据直接支持的最大范围。
- 禁止声明：列出不能由本次结果推导的能力。
- 下一门禁：写明可以进入的任务，或实际阻断项。

## 2. 环境与身份

| 项目 | 实际值 | 取证方式 |
| --- | --- | --- |
| OS / architecture |  |  |
| Python / uv |  |  |
| commit / worktree |  |  |
| `uv.lock` hash |  |  |
| 配置指纹 |  |  |
| 数据集 / manifest hash |  |  |
| baseline / gate hash |  |  |
| Provider / model / revision |  |  |
| request ID |  |  |

不得记录密钥、token、`.env`、原始客户数据、未脱敏提示词或 PII。

## 3. 实际执行命令

| # | 可复制命令 | 退出码 | 结果摘要 | 原始证据 / artifact |
| --- | --- | --- | --- | --- |
| 1 | `...` |  |  |  |

命令必须与实际执行一致。默认 skip 不得记为通过；Live/Enterprise 必须包含显式开关和非空已知 target。

## 4. 断言矩阵

| ID | 可观察断言 | 直接路径 | 绕过 / 失败路径 | 证据 | 结果 |
| --- | --- | --- | --- | --- | --- |
| A-01 |  |  |  |  | `passed | failed | blocked` |

关键权限、tenant 隔离、输入校验、幂等、副作用和引用完整性不能只覆盖 happy path。

## 5. 验证等级分离

| 卡片 | 目标 | 本次状态 | 证据边界 |
| --- | --- | --- | --- |
| Fixture | 确定性内存/Fake 契约 | `not-run | passed | failed | blocked` |  |
| Community | 真实开源本地组件 | `not-run | passed | failed | blocked` |  |
| Live | 真实公开 Provider/模型 | `not-run | passed | failed | blocked` |  |
| Enterprise / E-like | 真实企业 Adapter 或本地企业栈 | `not-run | passed | failed | blocked` |  |

Mock/Fake 通过只能支持 Fixture 声明；本地真实 SQLite/Chroma 可支持 Community 声明；没有真实 Provider request ID/model/revision 不能声明 Live 通过；没有企业环境证据不能声明 Enterprise 接入通过。

## 6. 安全、数据与副作用

- 信任边界与本次威胁 ID：
- 使用的数据类型（合成/脱敏/真实）：
- tenant/actor/executor 映射方式：
- 黑名单、营业状态、权限与提示词注入结果：
- 预期业务写入与实际计数：
- 日志/报告脱敏检查：

## 7. Artifacts 与可复现性

列出报告、JSON、构建产物和哈希的相对路径。临时目录不作为唯一证据；需要长期保留的脱敏证据放入 `reports/verification/<version>/<run_id>/`。

## 8. 已知限制与后续

- 未执行项：
- 失败项：
- `blocked_by`：
- 已接受的剩余风险：
- 后续任务 / 负责人：

## 9. 结果判定规则

- `passed`：本报告声明范围内的所有必需断言都有实际证据且通过。
- `failed`：必需断言已执行但不满足。
- `blocked`：必需断言因缺少 Key、模型、环境、权限或前置产物而无法执行。
- 一份 Core 报告的 `passed` 不会自动将 Live 或 Enterprise 卡变为 passed。
