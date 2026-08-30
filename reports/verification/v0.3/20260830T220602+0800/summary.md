# V0.3-T03 Fixture/Community 验证报告

- 任务：Business DB execution ledger、canonical args/plan hash、domain/audit/outbox、receipt、unknown/reconciliation 与单库事务边界
- 时间：2026-08-30 22:06:02 +08:00
- 验证层级：Fixture/Community，本地 SQLite、Mock Adapter 与合成数据
- 当前迁移头：`platform_0005` / `business_0003`
- 凭证、完整 prompt、思维链、外部响应原文、真实商家与客户数据：未写入报告

## 实现结果

- 新增严格不可变的 `ToolExecution`、`DomainEvent`、`OutboxRecord`、`Receipt`、`LaunchChildStep` 与 `LaunchPlan` 值类型。执行状态固定为 `reserved → executing → succeeded|failed|unknown`；`unknown` 只能通过对账收敛为 succeeded/failed，不能回到 executing。
- `LaunchPlan.compute_plan_hash()` 按 tool name 排序子步骤，并复用 ADR-024 的 `_normalize`/`_canonical_json` 规则，对规定的草案、规则、券批次、商家范围、素材、子步骤和补偿策略绑定做 UTF-8 SHA-256。整体 plan hash 不替代子步骤幂等键。
- `business_0003` 新增 `tool_executions`、`domain_events`、Business `audit_events` 与 Business `outbox`；runner 已分别登记 Business/Platform 同名表的完整列签名和外键签名，资源 manifest 与双层 SHA-256 已同步。
- SQLite Repository 覆盖原子 reserve、历史读取、执行与终态落账、unknown 对账、append-only domain/audit 事件和 outbox 待投递查询/发布。重复 `(tenant_id, tool_name, idempotency_key)` 返回既有记录，不增加执行次数。
- `ExecutionLedger` 在外部调用前提交 reservation，再标记 executing；终态业务写、ledger、domain/audit/outbox 使用同一个 Business session 事务。任一写入失败会整体回滚，不把 checkpoint、外部系统或两库伪装为分布式原子事务。
- ApprovalService 生成脱敏 Platform audit envelope；SQLite approval 状态与 Platform audit/outbox 使用同一个 Platform session 事务。Business 与 Platform 只通过 ID、幂等事件和对账关联，不跨库双写。
- receipt 只保留 ID 与脱敏摘要 hash；domain/audit 的敏感键在持久化前统一脱敏，未保存外部响应原文。

## 验证命令与真实结果

### 完整社区测试

```text
$ uv run pytest -m "not live and not enterprise and not performance" -q
420 passed, 1 deselected in 112.02s (0:01:52)
```

### 安全测试

```text
$ uv run pytest -m security -q
70 passed, 351 deselected in 14.29s
```

### 静态检查

```text
$ make lint
uv run ruff format --check .
186 files already formatted
uv run ruff check .
All checks passed!
uv run mypy src/oria
Success: no issues found in 98 source files
```

## 迁移、事务与恢复专项证据

- 覆盖空 Business DB 升级到 `business_0003`、重复 upgrade、降级到 `business_0002`、约束拒绝及 Platform/Business revision 链独立 rollback。
- 覆盖 reserve 后才调用 Mock Adapter；Adapter 调用期间可观察状态为 executing；重复进入直接返回历史结果。
- 覆盖 Business 状态更新后制造 domain event 冲突，验证业务状态、ledger 与 outbox 全部回滚。
- 覆盖 Platform approval 更新后制造 audit event 冲突，验证 approval、audit 与 outbox 单事务回滚。
- 覆盖 unknown 重入不调用 Adapter、禁止 unknown→executing、对账后收敛 succeeded 且 `attempt_count=1`。
- 覆盖 Business audit 仅落 Business DB、Platform audit 不增加记录，证明 ExecutionLedger 不跨库写入。
- 完整套件首次收口曾因离线 demo 的精确表清单仍为 T02 版本出现 `419 passed, 1 failed`；同步四张新表后重跑全绿。`make lint` 首次指出 migration 单行格式，修正后同步刷新 migration/manifest 双层 hash 并重跑全绿。

## 明确未做与后续边界

- 未实现 T04–T09 的业务 Tool、Launch saga、商品分支、选品、完整 Graph/CLI 与场景 S1–S6。
- 未把 plan hash 当作子步骤幂等键，也未声称 checkpoint 与外部系统具备跨系统事务原子性。
- 未新增运行时依赖，未修改 `uv.lock`，未运行真实网络、Live、Enterprise 或 Performance 测试。
