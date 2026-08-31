# V0.3-T05 Fixture/Community 验证报告

- 任务：商品库 Adapter/ProductEligibilityPolicy、三模式报名分支/确认链/券关联
- 时间：2026-08-31 08:23:37 +08:00
- 验证层级：Fixture/Community，本地 SQLite、内存 Mock Adapter 与全合成数据
- 当前迁移头：`platform_0005` / `business_0004`
- 本任务未新增 migration、运行时依赖或真实网络调用

## 实现结果

- 新增区别于 Business DB 脱敏引用实体的完整商品 `ProductSnapshot` 值，以及 `ProductCatalogAdapter` Protocol 和可稳定重放旧 snapshot/cursor 的内存 Mock。
- `ProductEligibilityPolicy` 只从完整性通过的冻结规则快照构造条件，确定性判定 Decimal 价格范围、类目、关键词和可用状态；返回稳定命中/排除 reason codes，LLM 不参与。
- `query_eligible_products` 校验 campaign 与 Business 规则引用，再校验 `rule_snapshot_id + product_circle_policy_ref/version`；游标绑定 catalog snapshot、merchant IDs 与策略版本，当前 catalog 切换后仍重放原快照后续页。
- 报名协调器实现 `merchant` 等关窗、`auto` 直接完成、`hybrid` 双分支 join；webhook 经 T02 `IntegrationEventInboxService` 验签/去重，不读取请求自带 checkpoint/wait ID。
- 关窗后默认仅在 inbox/分支状态记录 `late_rejected`，不修改已接受版本；冻结规则为 `new_version` 时递增 Enrollment version 并调用下游审批失效接口。
- `upsert_enrollment_items` 每次写前再鉴权，同时复核商家硬资格和商品硬规则；用现有业务唯一键、`merge_source` 和 `validate_sources_for_mode` 汇聚，按冻结 `BusinessConfirmationPolicy` 生成零到多级任务，不制造固定 HITL。
- `link_coupon_batch` 在同一 Business 事务内复核 tenant、campaign/rule ref、coupon version/status、冻结 benefit tier 和 EnrollmentItem 确认状态；业务写、execution ledger、domain/audit/outbox 同事务提交，部分失败时关联计数保持 0。

## 验证命令与真实结果

```text
$ uv run pytest -m "not live and not enterprise and not performance" -q
506 passed, 1 deselected in 82.99s (0:01:22)

$ uv run pytest -m security -q
84 passed, 423 deselected in 11.40s

$ make lint
uv run ruff format --check .
213 files already formatted
uv run ruff check .
All checks passed!
uv run mypy src/oria
Success: no issues found in 110 source files

$ uv run python -c "from oria.resources.loader import verify_migration_assets; print(verify_migration_assets())"
{'platform': 'platform_0005', 'business': 'business_0004'}
```

## 关键测试证据

- 商品四类硬过滤与来源模式：`tests/unit/test_v03_product_eligibility.py`。
- 同 snapshot 分页、游标重放与规则绑定：`tests/contract/test_v03_product_catalog.py`。
- Tool schema、报名/券关联幂等、冻结档位与无固定 HITL：`tests/contract/test_v03_enrollment_tools.py`。
- 三模式 join、双来源去重、重复/迟到 webhook 和新版本：`tests/integration/test_v03_enrollment_branches.py`。
- 窗口/确认链超时、券关联部分失败零悬空：`tests/integration/test_v03_enrollment_recovery.py`。
- 过滤覆盖拒绝、跨 tenant 商品/报名/券隔离：`tests/security/test_v03_enrollment_boundaries.py`。

## 提交

- `1337e6f` `feat: implement deterministic product enrollment workflow`
- `7a78c39` `test: cover product enrollment modes and recovery`

## 未验证边界

- 未运行 Live、Enterprise、Performance 或任何真实网络；只证明 Community 本地业务语义，不声称真实商品库/企业 Adapter 已接入。
- T07 才将 T04/T05/T06 工具按预定 Graph builder 注册到场景 A 完整 workflow/CLI；当前 V0.1 research Graph 的模型可见 allowlist 未扩权。
