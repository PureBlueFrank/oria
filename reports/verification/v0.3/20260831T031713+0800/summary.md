# V0.3-T04 Fixture/Community 验证报告

- 任务：活动草案、LaunchPlan 复合审批、券物化/招商投放子工具与 checkpointed launch saga
- 时间：2026-08-31 03:17:13 +08:00
- 验证层级：Fixture/Community，本地 SQLite、内存 Mock Adapter 与合成数据
- 当前迁移头：`platform_0005` / `business_0004`
- 凭证、完整 prompt、思维链、外部响应原文、真实商家与客户数据：未写入报告

## 实现结果

- `LaunchSagaState` 固定为 `planned → coupon_materialized → recruitment_published → completed`，仅允许转入 `compensation_pending / reconciliation_required / failed` 三个终态。`business_0004` 仅替换该表的数据库 CHECK，未新增列。
- `persist_campaign_draft` 在任何 Business 写入前复核规则 snapshot hash、tenant、Decimal 有限正值/折扣率/阶梯、带时区且内含的活动与报名窗口、`object://` 素材和非空文本；Campaign、规则引用、券草案和招商投放引用以单一 Business 事务写入，不调用 Adapter，不写 execution ledger/domain event/outbox。
- `LaunchPlan` 的 hash 现覆盖草案/规则/券的 ID 与 hash、范围 hash、素材版本、固定两个子步骤的 canonical args hash 与幂等范围、补偿策略版本。复合命令名固定为 `LaunchPlan`，审批恢复复用 `ApprovalService.authorize_resume` 的 action/tool/hash/checkpoint/policy 绑定。
- `materialize_coupon_batch` 与 `publish_recruitment` 分别使用 `stable_business_id:canonical_args_hash` 幂等键，按 `reserve_for_args → authorize_resume → execute` 执行；重复进入读取历史记录，不重放内存 Mock Adapter。成功业务写、ledger、domain/audit/outbox 在同一 Business 事务提交。
- saga 每步状态落库；若外部成功已落 ledger 但 saga checkpoint 尚未推进，恢复时读历史并继续。投放 `unknown` 直接进人工对账且不重投。
- 券已物化而投放失败时，CouponBatch 保持 `ready`。默认未验证补偿策略与 Adapter 契约会 fail-stop 到 `reconciliation_required`；仅当两者均声明已验证幂等时，才执行带独立 ledger/幂等键的补偿命令。本地 CouponBatch 不会被伪回滚成 `draft`。
- 新增四个受控 Tool 实现：`persist_campaign_draft`、`launch_approval`、`materialize_coupon_batch`、`publish_recruitment`。T04 将写路径服务挂入 `DomainServiceRegistry`/`build_runtime`；当前 V0.1 research Graph 的模型可见 allowlist 仍保持两个只读 Tool，待 T07 按 workflow builder 显式注册写工具，避免旧 prompt 获得写权限。

## 验证命令与真实结果

### 完整非 Live/Enterprise/Performance 测试

```text
$ uv run pytest -m "not live and not enterprise and not performance" -q
478 passed, 1 deselected in 79.67s (0:01:19)
```

### 安全测试

```text
$ uv run pytest -m security -q
81 passed, 398 deselected in 10.68s
```

### 静态检查

```text
$ uv run ruff format --check .
199 files already formatted

$ uv run ruff check .
All checks passed!

$ uv run mypy src
Success: no issues found in 103 source files
```

### 迁移资源完整性

```text
$ uv run python -c "from oria.resources.loader import verify_migration_assets; print(verify_migration_assets())"
{'platform': 'platform_0005', 'business': 'business_0004'}
```

## 验证门禁与测试证据

- 草案无外部副作用：`tests/contract/test_v03_campaign_draft.py::test_campaign_draft_writes_only_local_business_facts_without_ledger_or_outbox`。
- 规则/金额/日期/素材校验且失败无写入：`tests/unit/test_v03_campaign_launch.py::test_persist_campaign_draft_rejects_invalid_rules_without_any_write`（`rule-hash/amount/date/material-ref/material-text`）。
- LaunchPlan 任一顶层绑定或子步骤参数改变时 hash 变化：`tests/unit/test_v03_ledger_values.py::test_launch_plan_hash_changes_with_every_top_level_binding` 与 `test_launch_plan_hash_is_order_independent_and_binds_every_child_argument`。
- 草案成功后业务写/ledger/domain/audit/outbox 同事务及回滚：`tests/contract/test_v03_campaign_launch_contract.py::test_materialize_and_publish_use_independent_ledgers_and_replay_history_once` 与 `test_success_business_write_ledger_and_events_roll_back_together`。
- 审批篡改、换恢复主体、自批、未批准、跨 tenant：`tests/security/test_v03_campaign_launch_security.py::test_rejects_tampered_launch_plan_binding`、`test_rejects_tampered_launch_child_arguments_and_invalidates_approval`、`test_rejects_changed_approval_subject_and_invalidates_binding`、`test_launch_requester_cannot_self_approve`、`test_unapproved_launch_cannot_materialize_or_publish`、`test_cross_tenant_launch_resume_is_denied_without_side_effects`。
- 不伪回滚/未验证补偿进对账：`tests/integration/test_v03_campaign_launch_recovery.py::test_publish_failure_keeps_materialized_coupon_and_requires_reconciliation`。
- unknown 进对账且不重投：`tests/integration/test_v03_campaign_launch_recovery.py::test_unknown_publish_enters_reconciliation_and_is_never_blindly_retried`。
- 中间 checkpoint 恢复：`tests/integration/test_v03_campaign_launch_recovery.py::test_saga_resumes_from_unadvanced_checkpoint_using_child_ledger_history`。
- 仅已验证幂等补偿可执行且重放不重调 Adapter：`tests/integration/test_v03_campaign_launch_recovery.py::test_only_verified_idempotent_compensation_runs_and_replays_history`。

## 提交

- `0f93661` `feat: define launch saga state machine`
- `bd29d24` `feat: migrate launch saga statuses`
- `ee094c4` `feat: persist validated campaign drafts`
- `0677be2` `feat: execute approved campaign launch saga`
- `9089535` `test: cover launch tampering and recovery`
- `83867dc` `style: format launch migration test`

## 明确未做与后续边界

- 本任务未实现 T05–T09 的商品库/报名双分支/确认链/选品/C 端投放/通知/完整 Graph 与 CLI。
- 未把 plan hash 当作子步骤幂等键，未声称 Platform/Business/外部系统跨库原子。
- 未新增运行时依赖，未修改 `uv.lock`，未运行真实网络、Live、Enterprise 或 Performance 测试。Community 本地业务语义已用 Mock Adapter 验证，企业 Adapter 未验证。
