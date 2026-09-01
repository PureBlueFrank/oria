# V0.3-T06 P0 审查修复验证报告

- 任务：按权威审查报告修复 V0.3-T06 P0-1～P0-9
- 时间：2026-09-01 18:08:29 +08:00
- 验证层级：Fixture/Community，本地 SQLite、Mock Adapter 与合成数据
- 基线提交：`977ad9e`
- 代码完成提交：`49b06aa`
- 当前迁移头：`platform_0007` / `business_0009`
- migration manifest SHA-256：`26feca8e179cc1aada4abaa1b57602c6b5d22702f8ca06515303e45b5cfcd5d0`
- 工具链：`uv 0.12.6` + `Python 3.11.15` + `uv.lock`

## P0 修复结果

- P0-1：已修复并验证 migration 精确文件集、head、runner schema/FK、manifest 和 loader 哈希链。
- P0-2：已实现 SQLite assortment Repository、三个 Tool、Runtime/Domain registry 装配与受信 selection event service。
- P0-3：选品提交改为服务端候选集子集校验，同事务重放活动、确认链、券关联和冻结资格。
- P0-4：selection event 只能消费持久化 matched inbox，逐字段校验并 CAS 为 consumed；普通 campaign admin 无 apply 权限。
- P0-5：decision 必须属于 submission；completion 要求每个 item 恰有一个同版本决定，原子封存 selection version/hash，发布显式拒绝空集、重复、缺失、跨版本和 seal 不一致。
- P0-6：三条副作用路径在 reserve 前完成所有纯读 precheck/审批/鉴权；rejected 结果原子写入 failed ledger、audit 和 outbox。
- P0-7：executing 使用明确 timeout；未过期重放返回 waiting，过期则与 projection/audit/outbox 同事务转 `unknown/reconciliation_required`，不重调 Adapter。
- P0-8：普通 `BusinessMutation` 恢复为仅 `succeeded` 可用；failed/unknown 只能使用不可变 `OutcomeProjectionMutation`，ledger 校验 tenant/execution/aggregate/outcome，三个 SQLite factory 拒绝成功态和覆盖已成功实体。
- P0-9：Context 增加 orchestrator 注入的受信 checkpoint，Service 契约显式要求 `checkpoint_id`；approval create、resume 和 ledger reservation 绑定同一值，`run_id` 仅作关联字段。

## 验证命令与真实结果

```text
$ make lint
231 files already formatted
All checks passed!
Success: no issues found in 121 source files

$ make test
557 passed, 1 deselected, 4 warnings in 173.50s

$ uv run pytest -m security
92 passed, 466 deselected in 24.70s

$ uv run python -c "... verify_migration_assets() ..."
{'platform': 'platform_0007', 'business': 'business_0009'}

$ git diff --check
<无输出，退出码 0>
```

Community 全套的 4 条 warning 是既有 SQLite/Alembic downgrade 反射复合外键时的 `SAWarning`；对应 migration 测试均通过。

## 未验证边界

- 未运行 Live、Enterprise、Performance 或真实选品/C 端投放/IM Adapter；不得据此声明企业接入通过。
- V0.3-T07 Graph/interrupt/CLI 完整接线不属于本任务；本次只冻结了受信 checkpoint 的 Service/Tool 契约。
- 未运行 `make build` 或 `make smoke`，本报告不声明这两项通过。
