# ADR-003：SQLite 与 PostgreSQL 共用 Repository，PostgreSQL 以事务级 tenant context + RLS 隔离

- 状态：已接受
- 日期：2026-09-20
- 关联任务：V0.6-T01

## 背景

Community 已有 platform/business 两条 SQLite Alembic revision 链和一套 SQLAlchemy Repository。V0.6-T01 需要将同一持久化契约接到 PostgreSQL，同时保留 SQLite 单 worker 语义，且不允许连接池复用泄漏 tenant context。

## 决策

1. platform 和 business 使用显式、相互独立的 backend 与 URL；选择 PostgreSQL 但 URL 缺失/非 PostgreSQL，或两条链指向同一数据库时启动失败，不回退 SQLite。Production URL 必须显式启用 TLS `sslmode`。
2. 两个 backend 共用现有 Repository 类和公共 Protocol。只对少量 SQL/Alembic 方言差异做窄分支，不维护第二套业务逻辑。
3. PostgreSQL engine 使用 SQLAlchemy async engine 和连接池。Repository 从受信 Context 取得 tenant_id 后，在当前事务显式调用 `set_tenant_context(connection, tenant_id)` 执行 `set_config('oria.tenant_id', tenant, true)`；SQL bind 参数永不建立 RLS identity，同一事务不得切换 tenant。`SET LOCAL` 随 commit/rollback 失效，engine 事件和 pool check-in 同时清理连接元数据。
4. PostgreSQL revision 对所有 Oria tenant 表启用并强制 RLS；policy 仅允许 `tenant_id = current_setting('oria.tenant_id', true)` 的读写。无 context 时 deny-by-default。
5. `oria db upgrade --target platform|business|all` 程序化驱动 package resources 中的两条链；`oria data init` 继续复用同一 runner。

## 后果

- Community 的 SQLite 路径和公共 Repository 契约保持不变。
- PostgreSQL 必须使用非 `BYPASSRLS` 的最小权限运行角色；拥有 `BYPASSRLS` 的超级角色不是受支持的运行时身份。
- V0.6-T01 只验证空库/开发数据链路；非空 SQLite→PostgreSQL 存量迁移仍属于 V0.8-T01。

## 验证

- 配置选择、URL 脱敏/失败关闭、独立 revision 链和安装 wheel 契约。
- SQLite 全量 Community/Security 回归。
- 真实 PostgreSQL 必须单独验证 migration、同一 Repository、RLS 无 context 拒绝、跨 tenant 隔离和 pool checkout 后 context 清空；缺 DSN 时只能记为 blocked。

2026-10-17：原生本地 PostgreSQL 17.11 的隔离双库验证已通过，运行角色不是超级用户且没有 BYPASSRLS。实测修复了验收查询错误地从 `pg_tables` 读取 `forcerowsecurity` 的问题，改从 `pg_catalog.pg_class` 与 `pg_namespace` 查询 ENABLE/FORCE 标记，继续要求所有租户表和隔离 policy 齐备；没有放宽控制。六项真实负例覆盖 Platform/Business 的 ENABLE、FORCE 和 policy 缺失；官方 saver 补核跨租户、namespace、pending writes、父游标与列表。证据见 [本地 PostgreSQL 卡](../../reports/verification/v0.6/20261017-postgres-local/summary.md)。生产部署与双 worker/lease/fencing 不在本卡范围内。
