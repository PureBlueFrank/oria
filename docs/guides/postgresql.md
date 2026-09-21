# PostgreSQL 企业级持久化接入

Oria 的 Community 默认后端仍是 SQLite。企业环境可以分别为 Platform 与 Business 选择两个独立 PostgreSQL 数据库，并继续复用同一套 Repository 契约；LangGraph checkpoint 使用官方 `AsyncPostgresSaver`。

> 当前验证边界：后端选择、配置失败关闭、migration、Repository、RLS SQL 和 saver adapter 已通过 Contract/Community/Security 验证；仓库当前没有两个真实测试 DSN，因此真实 PostgreSQL Enterprise 验证仍为 `blocked`。详见 [V0.6-T01 验证卡](../../reports/verification/v0.6/20260920-t01/summary.md)。

## 1. 准备两个独立数据库

Platform 与 Business DSN 必须指向不同数据库。生产运行角色应使用最小权限且不得拥有 `BYPASSRLS`；生产 URL 必须显式启用 `sslmode=require`、`verify-ca` 或 `verify-full`。

```bash
export ORIA_PLATFORM_DATABASE_URL='postgresql://oria_platform:你的密码@db.example.com:5432/oria_platform?sslmode=verify-full'
export ORIA_BUSINESS_DATABASE_URL='postgresql://oria_business:你的密码@db.example.com:5432/oria_business?sslmode=verify-full'
```

不要把 DSN、密码或证书提交到仓库。配置文件只引用环境变量：

```yaml
storage:
  platform_db: postgres
  biz_db: postgres
  platform_url: ${ORIA_PLATFORM_DATABASE_URL}
  business_url: ${ORIA_BUSINESS_DATABASE_URL}
```

可直接复制 [配置示例](../examples/postgres.yaml)。

## 2. 执行 migration

```bash
uv run oria db upgrade \
  --config docs/examples/postgres.yaml \
  --target all
```

`--target` 可选 `platform`、`business` 或 `all`。选择 PostgreSQL 后，URL 缺失、格式无效、两个 DSN 指向同一数据库，或 Production URL 未启用 TLS 时都会失败关闭，不会静默回退 SQLite。

Oria 的 Platform/Business 表由各自 Alembic revision chain 管理；官方 `AsyncPostgresSaver` 的 checkpoint 表由 saver `setup()` 管理，不属于 Oria migration。

## 3. 运行真实 PostgreSQL 验证

测试必须使用两个可销毁的独立数据库，并显式打开 Enterprise target：

```bash
export ORIA_TEST_PLATFORM_POSTGRES_DSN="$ORIA_PLATFORM_DATABASE_URL"
export ORIA_TEST_BUSINESS_POSTGRES_DSN="$ORIA_BUSINESS_DATABASE_URL"

ORIA_RUN_ENTERPRISE=1 \
ORIA_ENTERPRISE_TARGETS=postgres \
uv run pytest -m enterprise \
  tests/integration/test_v06_postgres_enterprise.py -q -rs
```

只有该测试在真实 PostgreSQL 上执行并通过，才能证明 migration、双库 Repository、RLS 默认拒绝、连接池 tenant context 清理和官方 saver 的实际行为。缺少 DSN 导致的 skip/blocked、SQLite、Mock 或静态 SQL 断言都不算 PostgreSQL Enterprise 通过。

## 4. 接入边界

- 当前任务只交付 PostgreSQL 持久化基础，不代表 FastAPI、Durable Job、双 worker lease/fencing、SSE、Webhook 或企业 Adapter 已完成。
- SQLite 到 PostgreSQL 的非空存量迁移属于后续任务；空库建表不能称为存量迁移完成。
- 更完整的数据边界和恢复语义见 [架构概览](../../ARCHITECTURE.md)。
