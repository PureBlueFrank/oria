# V0.3 整体缺陷审计与修复验证报告

```yaml
run_id: "20260903T082513+0800"
version: "V0.3"
verification_level: "CT | IT | SEC | REC | C"
baseline_commit: "e8b9db4（工作树含本报告所述未提交修复及先前 V0.4-T01 变更）"
executed_at: "2026-09-03T08:25:13+08:00"
environment: "macOS / Python 3.11.15 / uv 0.12.6 / uv.lock sha256:b45a0bb6cef787031c371e3351d8bbad6b7b325388eb838c6265dc46ec5ad425"
migration_heads: "platform_0007 / business_0010"
migration_manifest_sha256: "a85e20a96ee0f03433d2e692e4cdca535c5769fb9d02b302b459cccae68e42b3"
result: "passed"
known_limits:
  - "未重跑 DeepSeek Live；V0.3-T09 仍引用已通过的 20260903T004622+0800 独立验证卡"
  - "未运行 Enterprise/Performance，真实券、招商、商品库、选品、C 端和 IM Adapter 仍未验证"
  - "SQLite 单 worker 结果不证明 PostgreSQL 多 worker/fencing 语义"
```

## 审计结论

对 V0.3-T01–T09 任务映射、迁移链、历史验证卡、当前静态检查和 Community 全量套件进行交叉检查；重点复核了集成事件 inbox、wait 恢复、execution ledger、租户隔离、审批绑定与迁移完整性。共复现 3 个可达缺陷，已全部修复并补充回归用例。

| ID | 缺陷 | 修复前证据 | 修复 |
| --- | --- | --- | --- |
| V03-BUG-01 | 未授权集成事件会先占 `(tenant, adapter, source_event_id)`，使后续真实事件被判为 duplicate，可造成恢复流程拒绝服务 | 新回归用例首次运行得到 `authenticated.status == duplicate`，1 failed / 10 passed | 未授权 envelope 在占用持久去重标识前 fail closed；后续合法事件可正常 matched |
| V03-BUG-02 | 事件在 wait 过期前分类为 matched，但过期后领取时未再校验 `expires_at`，可恢复已过期流程 | 新集成用例在 `consumed_at == expires_at` 时未拒绝，1 failed | `consume_matched` 在同一领取事务中重新校验过期时间，边界时刻及之后均拒绝 |
| V03-BUG-03 | Adapter 明确返回 `rejected` 时，failed ledger 丢失 receipt ID 和脱敏 summary hash，无法依赖该证据对账 | 新契约用例首次运行时无 `receipt_summary_hash` 列，且 failed execution 不保留 receipt | 新增 `business_0010`，保留已拒绝回执的 ID 与 SHA-256 摘要；异常失败仍允许无回执，旧数据兼容 |

## 验证结果

```text
$ .venv/bin/pytest tests/security/test_v03_integration_inbox.py \
  tests/integration/test_v03_platform_repositories.py \
  tests/contract/test_v03_execution_ledger.py \
  tests/integration/test_v03_business_migration.py \
  tests/integration/test_v03_execution_ledger_migration.py \
  tests/unit/test_v03_ledger_values.py -q
44 passed, 4 warnings

$ .venv/bin/pytest tests -k "v03 or t03" -q
290 passed, 321 deselected, 4 warnings

$ .venv/bin/pytest -m security -q
103 passed, 507 deselected

$ .venv/bin/pytest -m "not live and not enterprise and not performance" -q
610 passed, 1 deselected, 4 warnings in 128.59s

$ .venv/bin/ruff format --check .
248 files already formatted

$ .venv/bin/ruff check .
All checks passed!

$ .venv/bin/mypy src/oria
Success: no issues found in 130 source files

$ .venv/bin/python -c "... verify_migration_assets() ..."
{'platform': 'platform_0007', 'business': 'business_0010'}

$ UV_CACHE_DIR=/tmp/oria-v03-audit-uv-cache uv build
Successfully built dist/oria-0.1.0.tar.gz
Successfully built dist/oria-0.1.0-py3-none-any.whl

$ .venv/bin/oria --version
0.1.0

$ git diff --check
<无输出，退出码 0>
```

首次受限构建因沙箱 DNS 无法获取锁定的 `hatchling` 而失败；经批准联网后原命令成功。wheel 与 sdist 均已确认包含 `business_0010` 和 migration manifest。

4 条 warning 为既有 SQLite/Alembic downgrade 反射复合外键时的 `SAWarning`；对应升级、重复升级与降级用例全部通过。

## 最终口径

- V0.3-T01–T09 任务交付与必需 DeepSeek Live 卡仍为已完成；本轮另外关闭 3 个审计缺陷。
- 允许声明 V0.3 Community/Core 与必需 DeepSeek 草案/软排序 Live 已通过。
- 不得声明真实企业 Adapter 或多 worker Durable Job 已验证。
