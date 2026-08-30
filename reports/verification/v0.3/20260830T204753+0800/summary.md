---
run_id: "20260830T204753+0800"
version: "V0.3"
task_id: "V0.3-T01"
depends_on: ["V0.2-Core", "V0.1-T03"]
verification_level: "Fixture/Community UT + IT"
base_commit: "2cbb9fdd629529a917396ccb8bbaa626fdf68a22"
commit: null
worktree_state: "dirty；仅含阶段 D 测试、Demo schema 兼容修复、路线状态与本报告"
executed_at: "2026-08-30T20:47:53+08:00"
environment: "Darwin 25.6.0 x86_64 / Python 3.11.15 / uv 0.12.6"
network_executed: false
live_verified: false
enterprise_verified: false
result: "passed"
blocked_by: []
known_limits:
  - "V0.3-T02–T09 未实现；不得把 T01 的模型、migration 与 Repository 声明为完整 Workflow。"
  - "未运行 Live、Enterprise 或 Performance，也未连接真实券、招商、选品、C 端、通知或 IM 系统。"
  - "业务 Service/Tool、审批、event resume、outbox/execution ledger 与 S1–S6 属后续任务。"
---

# V0.3-T01 招商领域模型、迁移与 Repository 验证报告

## 结论

V0.3-T01 的 Fixture/Community 门禁通过。14 个不可变业务实体、Campaign/CouponBatch 状态机、`merchant|auto|hybrid` 与双来源幂等汇聚、`business_0002` 的 14 张 tenant-scoped 表，以及 14 个具名 Repository Protocol/SQLite 实现均已完成。本报告只证明 T01，不代表 V0.3 Core 或完整场景 A 已完成。

## 实际验证命令与输出

| 命令 | 真实结果 |
| --- | --- |
| `uv run pytest tests/integration/test_v03_business_migration.py -q` | `3 passed in 0.49s` |
| `uv run pytest -m "not live and not enterprise and not performance" -q`（首次） | `357 passed, 2 failed, 1 deselected in 70.46s`；发现旧 Demo 禁止 campaign/coupon schema 的过期断言 |
| `uv run pytest tests/integration/test_t08_demo.py tests/integration/test_v03_business_migration.py -q` | 兼容修复后 `7 passed in 8.36s` |
| `uv run pytest -m "not live and not enterprise and not performance" -q`（最终） | `359 passed, 1 deselected in 87.06s` |
| `uv run ruff check .` | `All checks passed!` |
| `uv run ruff format --check .` | `165 files already formatted` |
| `uv run mypy src/oria` | `Success: no issues found in 89 source files` |
| `uv build` | 成功构建 `dist/oria-0.1.0.tar.gz` 与 `dist/oria-0.1.0-py3-none-any.whl` |
| 全新 Python 3.11 venv 安装 wheel 后运行 `scripts/verify_t03_wheel.py --data-dir <临时目录>` | `verified installed wheel assets, current revisions, idempotent data init, and V0.3 business tables` |
| 同一隔离 wheel 运行 `scripts/verify_t08_wheel.py --work-dir <临时目录>` | 两次 Demo 成功；`business_side_effect_free=true`、10 个合格商家、两个只读工具 |
| `uv run python scripts/verify_t03_bypass_boundaries.py --work-dir <临时目录>` | 拒绝存储旁路、硬规则覆盖、受限字段序列化、migration 绕过、路径逃逸与资产篡改 |

## 关键断言

- 领域值：14 个实体统一要求非空 tenant、`version >= 1`、时区感知的创建/更新时间且时间不倒退；模型不可变。
- 状态机：Campaign 的八态路径与 active→cancelled 分支、CouponBatch 的三种物化结果和 expired 收口均通过；跳级或终态回退被拒绝。
- 幂等键：ProductSnapshot、CouponBatch、RecruitmentPublication、Enrollment、EnrollmentItem、EnrollmentCouponLink、ConsumerPlacement、MerchantNotification 的 ADR-026 复合唯一键直接从 SQLite index 元数据核对。
- tenant：14 张新表的每个外键都包含 `tenant_id → tenant_id`；Repository 使用可信 Context tenant，跨 tenant create/unique-key read 被拒绝。
- 可复核性：业务侧 `CampaignRuleSnapshotRef` 只保存 RAG `snapshot_id/snapshot_hash`，不复制六类规则；ProductSnapshot 固定商品 ref/version 与 catalog snapshot。
- Repository：全部实体完成真实 SQLite create/get/get-by-unique/upsert 回读；乐观锁冲突拒绝，EnrollmentItem 双来源 JSON 往返不丢失，Campaign/CouponBatch 裸改状态被拒绝。
- migration：空库到 head、V0.1 `business_0001` 到 `business_0002`、回滚到 V0.1/base 均通过，回滚到 V0.1 后历史 merchant 数据保留。

## 回归修复记录

首次完整套件的两个失败均来自 V0.1 只读 Demo 把 campaign/coupon 表“存在”误判为业务副作用。T01 必须安装这些空表，因此修复为继续比较 Demo 运行前后完整 business DB 指纹；表结构存在不再报错，任何实际表内容或 schema 变化仍会触发 `business_side_effect_detected`。对应 Demo 与 wheel/bypass 校验同步适配当前迁移 head。附加运行 bypass 脚本时还发现其候选集断言滞后于当前冻结合成规则，已与既有集成测试统一为 10 个合格商家，并成功复跑。

## 未做 / 受限项

- 未实现 V0.3-T02–T09 的审批、确认策略 Service、execution ledger/outbox、外部 Adapter、Tool、Graph、CLI Workflow 与恢复/对账。
- 未执行 V0.3-S1 十步闭环或 S2–S6 故障/安全场景；这些不能由 Repository IT 替代。
- 未运行真实网络、Live、Enterprise 或 Performance；企业 Adapter 状态保持未验证。
