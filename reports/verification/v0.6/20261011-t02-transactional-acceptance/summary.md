# V0.6-T02 事务化飞书审批受理验证草稿

2026-10-12 校正：下文零 audit 的解析结论来自未装配 AuditService 的替身。真实运行时解析会记录授权审计；业务决定/receipt/replay/outbox 不变。处理期间撤权与 Policy 拒绝审计缺口已补回归，失败历史及修复验证见 [审查修复验证卡](../20261012-review-fixes/summary.md)。

- 日期：2026-10-11
- 状态：定向 Community/Fixture 验证通过；V0.6-T02 仍为 **IN PROGRESS**
- 范围：本地 SQLite、Mock/Fixture runtime、合成飞书签名回调与本地 ASGI HTTP
- 独立最终验证: Community 1067 passed / 3 deselected; Security 170 passed / 900 deselected (与 Community 有重叠, 不相加); make lint 全通过; uv build 成功; 最新 wheel 离线安装后 t02-t08 与 v02_t05 共 8 个安装验证脚本全部 exit 0。
- 证据: community-final.log、security-final.log、lint-final.log、wheel-final.log。
- wheel 初次失败为网络连接重置; 后续 t08 参数错误已改为 --work-dir 并复验, 保留失败日志。
- 未运行: Live、Enterprise/PostgreSQL、真实飞书、远程投递、Job/wait/Graph 恢复。代码仍未提交或推送, 本报告不代表 T02 完成。

## 结论

`TransactionalFeishuReceiver` 现在显式向受信 acceptor 传递已提交 duplicate 事实和原 receipt_ref。`TransactionalApprovalCardAcceptor` 只使用 receiver 提供的 `AsyncSession`，通过 session-bound approval repository 复用 `ApprovalService` 的 Policy、职责分离、过期、冻结策略版本和完整快照 CAS 逻辑。新 action 的 receipt/replay、审批决定、审批审计和 outbox 一起提交或回滚；已提交 action 重试重查当前身份/Policy/SOD/binding，但不要求旧 pending revision，且不产生第二次决定、审计或 outbox。

`platform_0011` 在未提交状态中增加了服务端 `decision_intent` 与 approve-only CHECK 约束。既有 `issue(...)` 调用保持兼容，但默认意图会显式持久为 `approve`；非 approve 意图失败关闭。带理由的 reject 契约未实现。

## 回归与证据

实现前回归：

```text
uv run pytest -q tests/integration/test_v06_approval_cards.py
1 failed, 1 passed, 1 warning in 11.12s
```

失败为 `decision_intent` 列不存在，与审查缺口一致。

最终定向测试：

```text
uv run pytest -q \
  tests/integration/test_v06_transactional_approval_acceptance.py \
  tests/integration/test_v06_approval_cards.py \
  tests/integration/test_v06_ingress_receipts.py \
  tests/integration/test_v06_approval_race.py \
  tests/security/test_v06_approval_api.py \
  tests/security/test_v03_approval_rbac.py \
  tests/integration/test_v03_approval_invalidation.py \
  tests/unit/test_v03_approval_values.py \
  tests/integration/test_v03_platform_migration.py \
  tests/integration/test_t03_data_init.py \
  tests/unit/test_t03_resources_and_eligibility.py \
  tests/security/test_t03_bypass_boundaries.py
92 passed, 1 warning in 49.59s
```

覆盖的关键行为：真实签名本地 HTTP 回调、成功精确一次决定/审批审计/outbox 与 receipt_ref correlation、同 action 并发与重启重试、CAS/审计/outbox 之后故障回滚及重试、两个 action_ref 竞争、HTTP 与卡片 CAS 竞争、当前撤权/角色/Policy/SOD/binding 重查、tenant/chat/message/sender/revision/回调过期/绑定过期/审批过期、策略版本失效化、非 approve 意图约束，以及只读 `resolve()` 零 receipt/replay/audit/outbox 副作用与 binding 不变。

事务受理 E2E 使用真实 `RuntimeServices` + 真实 SQLite repositories；只读 `resolve()` 的零副作用检查使用轻量 runtime stub（但 Policy 与 SQLite repository 为真实实现）。JWT 对象只是为启用 FastAPI 的飞书路由而注入的未调用 Mock transport，本卡不声明 JWT 网络链路。

静态检查：

```text
ruff format --check（受影响 9 个文件）: 9 files already formatted
ruff check（受影响源码/测试）: All checks passed!
mypy src/oria: Success: no issues found in 179 source files
git diff --check: passed
```

完整性摘要：

```text
platform_0011: d42cf09bc655e2fdcabed72e11cce72bd845d52d38d55cc20391bd338f8a6353
manifest.json: 61f40efa05eb41c9aaaf978cbbb2bc4edff7d8e4d46ad0f7fef0b1ecf4671ac4
```

## 边界与未完成项

- 默认 API 仍不装配 receiver，缺失时返回 503；本卡只用测试注入 receiver。
- 未启动 Job、wait 或 Graph 恢复，未猜测 wait/checkpoint 绑定。
- 未发送真实飞书消息，未使用网络通道，未运行 Live/Enterprise/PostgreSQL。
- 完整套件与版本门禁数字留给 orchestrator，本草稿不用定向结果宣称 V0.6-T02 或 V0.6 已完成。
