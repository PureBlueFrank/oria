# V0.3-T05 六项非阻断 P1 收口验证报告

- 任务：收口 T05 P1-3～P1-8，并执行完整本地门禁
- 时间：2026-09-01 08:26:03 +08:00
- 验证层级：Fixture/Community，本地 SQLite、内存商品 Adapter 与全合成数据
- 基线提交：`842d0e82b5caf5241b1b710bc7a335fcadcd8792`
- 工作区状态：本报告对应未提交修改
- 当前迁移头：`platform_0006` / `business_0008`
- 本次未新增 migration、依赖或真实网络业务调用

## 修复结果

- P1-3 adapter snapshot 回查：商品 Adapter 增加按 `catalog_snapshot_id` 回查保留快照的契约；自动圈品使用服务端签发的 circle-run binding 重载原快照，catalog 当前版本推进后仍可确定性提交，缺失或游标不匹配时拒绝。
- P1-4 auto/window 状态约束：自动分支提交前校验 tenant、窗口未开启、已关窗/已过期及已完成状态，非法状态均在业务写入前拒绝。
- P1-5 bundle 跨实体校验：Repository 在落库前统一校验 Enrollment、EnrollmentItem、ProductSnapshot 与 ConfirmationTask 的 tenant、merchant、product、snapshot 和 item 关联，避免依赖晚到的数据库外键异常。
- P1-6 审计 policy version：报名与券关联审计改为记录最终事务内重新鉴权返回的真实 `policy_version`，不再写固定占位版本。
- P1-7 ToolResult ledger key：主实现已返回 ledger 生成的持久幂等键；新增契约断言，明确其等于领域结果的 ledger key，且不等于 caller request key。
- P1-8 集成测试持久边界：报名分支集成测试改用真实 SQLite inbox、external wait、approval invalidation Repository 与 consumer；仅故障注入用例保留目标接口替身。

## 验证命令与真实结果

修复前先运行新增回归，得到 `12 failed, 11 passed`，确认六类缺口可复现。修复后结果如下：

```text
$ uv run pytest tests/contract/test_v03_enrollment_tools.py tests/integration/test_v03_enrollment_transaction_eligibility.py tests/integration/test_v03_enrollment_branches.py -q
23 passed in 7.06s

$ uv run pytest tests/contract/test_v03_product_catalog.py tests/contract/test_v03_enrollment_tools.py tests/integration/test_v03_enrollment_branches.py tests/integration/test_v03_enrollment_recovery.py tests/integration/test_v03_enrollment_transaction_eligibility.py tests/integration/test_v03_approval_invalidation.py tests/security/test_v03_enrollment_boundaries.py -q
42 passed in 10.32s

$ make test
534 passed, 1 deselected, 4 warnings in 88.85s

$ uv run pytest -m security -q
89 passed, 446 deselected in 13.16s

$ make lint
221 files already formatted
All checks passed!
Success: no issues found in 115 source files

$ make build
Successfully built dist/oria-0.1.0.tar.gz
Successfully built dist/oria-0.1.0-py3-none-any.whl

$ uv run python -c "from oria.resources.loader import verify_migration_assets; print(verify_migration_assets())"
{'platform': 'platform_0006', 'business': 'business_0008'}

$ make smoke
0.1.0

$ git diff --check
<无输出，退出码 0>
```

首次 `make build` 在受限沙箱中因无法解析 PyPI 域名而失败；获准联网后以相同命令成功。另行检查 wheel 与 sdist 文件清单，二者均未包含 `.artifacts` 缓存目录。

完整测试中的 4 条 warning 是既有 SQLite/Alembic downgrade 反射复合外键时的 `SAWarning`；所有对应迁移测试通过，本次未改 migration。

## 未验证边界

- 未运行 Live、Enterprise、Performance 或真实商品库/企业 Adapter；本报告只证明本地 Fixture/Community 行为。
- T07 Graph/CLI 完整招商流程尚不属于本次范围，不能据此声明 V0.3 十步闭环已完成。
