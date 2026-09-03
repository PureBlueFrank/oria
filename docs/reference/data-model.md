# 数据模型与核心表

Oria 的本地 Community 运行时数据集中在 `data_dir`：

```text
<data_dir>/
├── sqlite/platform.db
├── sqlite/business.db
├── chroma/
├── objects/
└── reports-tmp/
```

Platform DB 保存知识目录、审批、外部等待、集成事件与平台审计；Business DB 保存商家和招商领域事实。LangGraph 官方 Saver 在 Platform SQLite 中管理自己的 checkpoint 表，这些表不由 Oria Alembic migration 管理。Chroma 和对象目录是可从 catalog 重建的投影/内容存储，不代替领域事实。

## 13 组核心表

以下字段摘要与 `src/oria/migrations/runner.py` 的 `_EXPECTED_COLUMNS` 一致；完整约束以 Platform/Business migration 和领域模型为准。

| 数据库与表名 | 关键字段/约束 | 用途 |
| --- | --- | --- |
| Business `campaigns` | `tenant_id`, `campaign_id`, `rule_snapshot_ref_id`, `enrollment_mode`, `status`, `version` | 保存活动、冻结规则引用、报名模式和状态机版本。 |
| Business `coupon_batches` | `tenant_id`, `coupon_batch_id`, `campaign_id`, `coupon_spec_hash`, `status`, `version` | 保存活动券批次、规范化券参数哈希和物化状态。 |
| Business `enrollment_items` | `campaign_id`, `merchant_id`, `product_ref`, `product_version`, `product_snapshot_id`, `sources_json`, `status` | 以 tenant+活动+商家+商品版本唯一汇聚自主报名与自动圈品。 |
| Business `confirmation_tasks` | `enrollment_item_id`, `subject_type`, `subject_id`, `sequence`, `due_at`, `timeout_action`, `status` | 保存 merchant → sales → sales_manager 等动态确认步骤、主体和超时语义。 |
| Business `assortment_submissions` / `selection_decisions` | 提交表绑定 `submission_version` 和 `assortment_policy_ref/version`；决定表记录 `selection_version`, `enrollment_item_id`, `decision`, `reason_code` | 分别保存异步选品请求/封存结果和逐商品选品决定。 |
| Business `consumer_placements` | `campaign_id`, `selection_version`, `placement_spec_hash`, `status`, `request_id`, `receipt_id` | 保存 C 端投放请求、绑定参数、状态与外部回执。 |
| Business `merchant_notifications` | `merchant_id`, `campaign_id`, `result_version`, `template_id`, `channel`, `status`, `attempt_count`, `receipt_id` | 以商家+结果版本+模板+通道去重，跟踪重试、死信与送达回执。 |
| Business `tool_executions` | `execution_id`, `tool_name`, `idempotency_key`, `canonical_args_hash`, `checkpoint_id`, `status`, `receipt_id`, `attempt_count` | 副作用 execution ledger；支持 reserve-first、幂等重放、未知结果对账与回执证明。 |
| Business `domain_events` | `aggregate_type`, `aggregate_id`, `event_type`, `event_version`, `payload_json`, `correlation_id` | append-only 领域事实，用于业务回放、投影与对账。 |
| Platform `approvals` | `approval_action`, `tool_name`, `canonical_args_hash`, `checkpoint_id`, `policy_version`, `expires_at`, `requester`, `decider`, `decision` | 保存两道 HITL 的参数/checkpoint/策略绑定、决定、职责分离与有效期。 |
| Platform `external_waits` | `wait_id`, `event_type`, `resource_type/id`, `expected_version`, `checkpoint_id`, `expires_at`, `timeout_action`, `status` | 保存报名关窗、选品结果等外部等待及其受信恢复绑定。 |
| Platform `integration_event_inbox` | 复合主键 `(tenant_id, adapter_id, source_event_id)`；`event_type`, `resource_version`, `payload_hash`, `processing_status`, `wait_id` | 对已验签事件去重，仅保存脱敏 payload，校验资源/版本/等待后再恢复 Graph。 |
| 两库 `audit_events` / `outbox` | Audit 含 actor/action/resource/decision/policy/hash/result/correlation；Outbox 含 topic/payload/available/published/attempt/error | 在所属库内与源状态同事务保存安全审计与待投递事件，避免伪造跨库原子性。 |

## 关键关系与真相源

- 领域业务表通过 `tenant_id` 与复合外键/唯一约束阻止跨租户关联，通过 `version` 实现乐观锁。
- `campaigns` 是招商活动主聚合；券、报名项、选品提交、C 端投放和通知以稳定业务 ID 与它关联。
- Checkpoint 是 resume/pending writes/time-travel 的执行状态真相源；`domain_events` 是业务事实真相源；两库 `audit_events` 是安全审计事实。
- Business 写入+ledger+domain/audit/outbox 在 Business DB 内提交；审批状态+platform audit/outbox 在 Platform DB 内提交。跨库仅用 ID、幂等消费和对账投影最终一致。
- 外部券、招商、选品、投放和 IM 调用使用 reservation + idempotency key + receipt reconciliation；`unknown` 不盲目重试。

## 进一步阅读

- [本地 Workflow 操作手册](../guides/local-workflow.md)
- [V0.3 场景 A 威胁模型](../security/V0.3场景A威胁模型.md)
- [Oria 详细执行路线](../Oria详细执行路线.md)
