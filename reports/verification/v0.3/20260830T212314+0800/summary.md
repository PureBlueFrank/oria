# V0.3-T02 Fixture/Community 验证报告

- 任务：审批绑定、IntegrationEventInbox、写操作 RBAC/职责分离、BusinessConfirmationPolicy
- 时间：2026-08-30 21:23:14 +08:00
- 验证层级：Fixture/Community，本地 SQLite 与 Mock/合成数据
- 当前迁移头：`platform_0005` / `business_0002`
- 原始 body、凭证、真实商家与客户数据：未写入报告

## 实现结果

- canonical args hash 在 Pydantic 校验后规范化 Decimal、UTC 时间和集合，再对 tool 名、schema version 与参数做确定性 SHA-256；未知字段、NaN、Infinity、无时区时间与不可规范化值拒绝。
- `ApprovalService` 绑定 tenant、gate、tool、参数 hash、checkpoint、policy version 与过期时间；创建、异主体决策和恢复前均重新鉴权。自批、越权、跨 tenant、过期与任一绑定变化均拒绝，launch 与 consumer publish 闸门不可复用。
- `BusinessConfirmationPolicy` 从完整性校验通过的冻结 CampaignRuleSnapshot 生成零到多级任务；步骤唯一且按 merchant→sales→sales_manager 排列。reject、escalate 和显式授权后的 auto-confirm 超时语义已覆盖，缺省不自动通过。
- `platform_0005` 新增 approvals、external_waits、integration_event_inbox；迁移 runner、资源完整性清单及全部当前 head 断言同步更新。
- Inbox 只持久化结构化脱敏 payload、payload hash、验签主体与处理状态；唯一键为 `(tenant_id, adapter_id, source_event_id)`。未知/错误 schema 在持久化前拒绝；重复、未授权、无 wait、类型/资源不匹配、stale/out-of-order 和过期 wait 均不具备恢复资格。

## 验证命令与真实结果

### 完整社区测试

```text
$ uv run pytest -m "not live and not enterprise and not performance" -q
405 passed, 1 deselected in 121.68s (0:02:01)
```

### 安全测试

```text
$ uv run pytest -m security -q
70 passed, 336 deselected in 13.93s
```

安全套件首次运行曾出现 `69 passed, 1 failed`：未知 action 仍被正确拒绝，但拒绝原因文案从既有稳定文本被改短，破坏历史契约。恢复原文案后按同一命令重跑得到上述全绿结果。

### 静态检查

```text
$ uv run ruff check . && uv run ruff format --check . && uv run mypy src/oria
All checks passed!
177 files already formatted
Success: no issues found in 94 source files
```

### 构建与隔离安装 wheel

```text
$ uv build
Successfully built dist/oria-0.1.0.tar.gz
Successfully built dist/oria-0.1.0-py3-none-any.whl

$ uv venv <临时目录>/venv --python 3.11
$ uv pip install --python <临时目录>/venv/bin/python dist/oria-0.1.0-py3-none-any.whl
$ cd <临时目录>
$ <临时目录>/venv/bin/python scripts/verify_t03_wheel.py --data-dir <临时目录>/data
verified installed wheel assets, current revisions, idempotent data init, and V0.3 business tables from <临时目录>/venv/lib/python3.11/site-packages/oria/__init__.py
```

实际隔离环境使用 CPython `3.11.15`，验证脚本从 `site-packages` 导入，不依赖源码目录。

## 迁移与安全专项证据

- `platform_0004 → platform_0005`、空库到 head、`platform_0005 → platform_0004` 与回滚到 base 均由 integration 测试执行。
- 审批与 wait 均使用 tenant 复合主键；Inbox 使用规定的三列复合主键，并以 tenant 复合外键关联 wait。
- 全仓 `platform_0004` 搜索后，仅保留历史 revision/down_revision、V0.2 历史迁移断言和本任务 0004→0005 升回滚引用；当前 head 断言均为 `platform_0005`。
- 未新增运行时依赖，未修改 `uv.lock`，未执行真实网络模型、Live、Enterprise 或 Performance 测试。

## 明确未做与后续边界

- 未把 Inbox 的 `resume_eligible` 接到 Graph/Job CAS 恢复；该接线属于 V0.3-T07。
- 未实现 execution ledger、Platform audit/outbox 与审批状态同事务、Business domain/audit/outbox 或跨库对账；这些属于 V0.3-T03。
- 未实现 LaunchPlan/C 端业务 Tool、Saga、选品与完整 10 步场景；这些属于 V0.3-T04–T07。
- 未声称企业 Adapter 或真实外部系统已接入。
