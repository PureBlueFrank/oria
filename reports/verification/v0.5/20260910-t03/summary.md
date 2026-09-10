# V0.5-T03 验证卡

- 日期：2026-09-10
- 级别：Fixture / Community / Security
- 结果：passed
- 网络：未运行真实网络、Live、Enterprise 或 Performance

## 范围

- 在既有 `LocalPolicyEngine` RBAC/职责分离之后叠加 organization/region/labels ABAC deny 和属性 constraints。
- 保留静态 ToolRegistry allowlist，建立 PolicyDecision 驱动的模型可见工具子集。
- 实现并挂载 input prompt、input RAG、tool authorization 和 output safety 四个 Guardrail。
- 工具执行前重新鉴权；Prompt/RAG 命中只告警，不作为授权依据。

## 命令与结果

1. `make lint`
   - `ruff format --check`：318 files already formatted
   - `ruff check`：All checks passed
   - `mypy src/oria`：Success，159 source files
2. `uv run pytest -m "not live and not enterprise and not performance" -q`
   - 953 passed, 1 deselected, 4 warnings, 434.45s
   - warning 为既有 SQLite migration 的 SQLAlchemy PRAGMA foreign-key 解析警告。
3. `uv run pytest -m security -q`
   - 116 passed, 838 deselected, 68.44s

## 失败历史与修正

首次完整非 Live 运行为 926 passed、27 failed、1 deselected、4 warnings。主因是将普通分析工具参数 `region` 误解为策略资源属性，使原有无属性主体被错误拒绝；同时过宽的电话正则会脱敏 ISO 日期，破坏归因证据对账。修正后：

- ABAC 只消费受信授权调用方显式写入的 `AuthorizationContext.attributes`，不从模型工具参数推断。
- 输出电话脱敏收紧为明确手机号形态，保留 ISO 日期和证据 ID。
- 缺失主体的畸形授权请求在属性 constraints 生成前 fail closed。
- `local-v2` 策略版本相关审批与边界 fixture 已同步。

修正后对原失败类别定向复验 9 passed，随后重跑全部必需门禁得到上述最终结果。

## 边界

- 本卡不声称 Live 模型质量或 Enterprise 接入通过。
- 无数据库 migration、无新运行时依赖、无静态 allowlist 变更。
- Subagent 路由/handoff 属于 V0.5-T04；T03 提供的动态子集和执行前重新鉴权是其权限不放大的底层门禁。
