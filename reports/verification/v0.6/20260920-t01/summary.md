---
run_id: "20260920-t01"
version: "V0.6"
task_id: "V0.6-T01"
verification_level: "contract | community | security; enterprise blocked"
commit: "bc704cb088e3a98d5e813e2bc02e0c0b7e5902ed + scoped V0.6-T01 working tree"
worktree_state: "dirty; V0.6-T01 implementation, tests, docs, lock, and this report"
executed_at: "2026-09-20+08:00"
environment:
  os: "macOS 26.6.1 (25G76) x86_64"
  python: "3.11.15"
  uv: "0.12.6 (7938ca5d5)"
  install_mode: "source plus isolated wheel"
migration_heads:
  platform: "platform_0009"
  business: "business_0011"
dependency_lock_sha256: "530def2706457f3edb08d74d7f116d7b463702345d14b5d0503fc1b85eec048d"
migration_manifest_sha256: "63a79bdf0bd8bd8472d2ec40671ddf104323440771df42d2f896d7721571b1a4"
result: "blocked"
blocked_by:
  - "ORIA_TEST_PLATFORM_POSTGRES_DSN was not provided."
  - "ORIA_TEST_BUSINESS_POSTGRES_DSN was not provided."
known_limits:
  - "No real PostgreSQL server/container was exercised; SQLite or mocks are not PostgreSQL proof."
  - "T02+ API, Feishu, Job, worker, SSE, and webhook work is outside this task and was not implemented."
---

# V0.6-T01 PostgreSQL 持久化基础验证卡

## 1. 结论

- Community/Contract/Security 结果：`passed`。SQLite 单 worker 默认行为保持，后端选择、URL 失败关闭、双 migration chain、统一 Repository 契约、官方 saver adapter 契约和安装包资源已验证。
- PostgreSQL E-like 结果：`blocked`。当前环境没有两个独立测试 DSN，所以未验证真实 PostgreSQL migration、Platform/Business Repository、RLS 默认拒绝、连接池 context reset 和 `AsyncPostgresSaver`。
- 总卡片因必需的真实 PostgreSQL 证据缺失判定为 `blocked`，不把 SQLite、Mock 或静态 SQL 断言提升为 PostgreSQL 通过。

## 2. TDD RED / GREEN 证据

| 阶段 | 命令 | 退出码 | 结果 |
| --- | --- | --- | --- |
| RED-1 | `uv run pytest tests/contract/test_v06_postgres_persistence.py tests/contract/test_v06_checkpoint_adapter.py -q` | 2 | collection error：`TenantCheckpointSaver` 尚不存在 |
| RED-2 | `uv run pytest tests/contract/test_v06_postgres_persistence.py::test_two_postgres_revision_chains_require_distinct_databases -q` | 1 | `DID NOT RAISE`，两条 PostgreSQL revision chain 尚未防止同库冲突 |
| GREEN-2 | 同上 | 0 | `1 passed` |
| GREEN-final | `uv run pytest tests/contract/test_v06_postgres_persistence.py tests/contract/test_v06_checkpoint_adapter.py tests/integration/test_v06_postgres_enterprise.py -m 'not enterprise' -q` | 0 | `8 passed, 2 deselected in 5.51s` |

RED 先固定缺失的 saver adapter 和双库 fail-closed 行为，再实现最小契约并回归。真实 PostgreSQL 测试只在显式 DSN 下运行，没有用 fake 制造 GREEN。

## 3. 最终验证输出

| 命令 | 退出码 | 精确结果 |
| --- | --- | --- |
| `make lint` | 0 | `337 files already formatted`; `All checks passed!`; `Success: no issues found in 166 source files` |
| `uv run pytest -m "not live and not enterprise and not performance" -q` | 0 | `999 passed, 3 deselected, 4 warnings in 625.67s (0:10:25)` |
| `uv run pytest -m security -q` | 0 | `118 passed, 884 deselected in 75.45s` |
| `make build` | 0 | 成功生成 `oria-0.1.0.tar.gz` 和 `oria-0.1.0-py3-none-any.whl` |
| 隔离 venv 安装 wheel，运行 `scripts/verify_t03_wheel.py` | 0 | wheel assets、current revisions、幂等 data init 与 V0.3 business tables 验证通过 |
| wheel 环境 `oria db upgrade --target all` | 0 | `Platform database: platform_0009`; `Business database: business_0011` |
| wheel 环境 `oria --version` | 0 | `0.1.0` |
| `ORIA_RUN_ENTERPRISE=1 ORIA_ENTERPRISE_TARGETS=postgres uv run pytest -m enterprise tests/integration/test_v06_postgres_enterprise.py -q -rs` | 0 | `2 skipped in 5.00s`；两项均明确报告缺少 `ORIA_TEST_PLATFORM_POSTGRES_DSN` |

4 条 warning 来自 SQLite/Alembic batch migration 通过 PRAGMA 反射复合外键时的已有 `SAWarning`，本任务没有将其记为 PostgreSQL 证据。

## 4. 实现与断言矩阵

| 范围 | 实现 / 断言 | 状态 |
| --- | --- | --- |
| 后端选择 | Platform/Business 各自选择 SQLite 或 PostgreSQL async SQLAlchemy engine/session factory；生产 URL 缺失或 TLS 不安全时 fail closed | Community/Contract passed |
| Repository | 既有公开 Repository 类复用同一 SQLAlchemy 契约，仅对方言差异作窄适配；无第二套业务逻辑 | SQLite passed; real PostgreSQL blocked |
| tenant 隔离 | 事务级 `oria.tenant_id`、强制 RLS、pool check-in 清理，无 tenant context 默认拒绝 | Contract passed; real PostgreSQL blocked |
| Checkpoint | adapter 完整委托 `aput`/`aput_writes`/`aget_tuple`/`alist`，保留 namespace、parent、pending writes 与租户编码 thread key；PostgreSQL 表由官方 saver 拥有 | Contract passed; real saver blocked |
| Migration/CLI | Platform/Business 独立 head，`db upgrade --target platform\|business\|all`，`data init` 复用同一 runner，包内资源可用 | SQLite/wheel passed; real PostgreSQL blocked |
| 依赖 | 仅新增 `psycopg[binary,pool]` 和 `langgraph-checkpoint-postgres`；未新增 FastAPI/uvicorn | lock verified |

## 5. 证据边界与后续

- 测试 DSN：`ORIA_TEST_PLATFORM_POSTGRES_DSN` 和 `ORIA_TEST_BUSINESS_POSTGRES_DSN`；必须指向两个独立数据库。报告不保存 DSN、用户名、密码或 bearer token。
- 两个测试 DSN 均为 unset，当前主机也无 Docker CLI；因此 PostgreSQL 版本、真实连接池复用、RLS 策略执行和官方 saver 建表行为都没有实测结论。
- T02/Feishu/API/Job/worker/SSE/webhook 未实现；本卡不声明 V0.6 Core 完成。
- 未提交或推送；记录的 commit 为本次工作前基线。
