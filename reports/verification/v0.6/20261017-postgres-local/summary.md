# V0.6-T01 本地真实 PostgreSQL 验证

日期：2026-10-17；任务 V0.6-T01，依赖 V0.5-Core / V0.3-T01；**本地 E-like 结果 passed，原缺 DSN 阻塞已解除**。T02 仍进行中，未满足 T03 主线依赖。

## 实际环境

- macOS 原生 PostgreSQL **17.11**，从 [PostgreSQL 官方源码目录](https://ftp.postgresql.org/pub/source/v17.11/)下载，使用官方 SHA-256 校验文件核验；源码哈希 `dd27f2b3c59e73ed14aa3324901242bf69a032a6347805f274e6260322d42979`。
- 四轮分别使用全新临时集群和两个独立空数据库，均未连接已有业务库；每轮结束通过本集群的 `pg_ctl` 停止实例。没有安装系统服务，没有删除用户数据。原生本地部署，不是 Docker 或生产环境。
- 运行角色 `superuser / bypassrls / createdb / createrole` 均为 **false**；角色是隔离测试库的 owner，FORCE RLS 对 owner 同样生效。管理员仅用于新集群初始化及环境记录，不运行 Oria 迁移/Repository/saver。
- 服务绑定 127.0.0.1 临时端口，主机连接使用 SCRAM 密码验证。管理 socket 位于仅当前用户可访问的临时目录。随机测试密码仅在执行进程内传递；报告不保存 DSN、数据库用户名或密码。
- 构建选项关闭 ICU、readline、zlib、lz4、zstd、NLS，未启用服务端 TLS；仅用于本地合成数据验证。Production URL 必须显式启用 TLS 的既有规则保持不变，本卡不证明生产 TLS、区域排序、压缩、备份或性能。
- uv 0.12.6 / Python 3.11.15，复用项目锁定环境。依赖版本、锁文件和最终源码/测试哈希见 [source-snapshot.json](source-snapshot.json)。没有安装个人 Python 依赖或更改锁文件。

PostgreSQL 官方 macOS 下载页允许原生发行方式，见 [macOS packages](https://www.postgresql.org/download/macosx/)。本轮先尝试该页面列出的 Postgres.app 17 二进制下载，因传输超时未使用；改用上述校验后的官方源码完成构建。下载和编译均在临时目录，未放入仓库发行物。

## 真实缺陷与修复

原 `_validate_postgres_rls` 从 `pg_tables` 查询不存在的 `forcerowsecurity`，迁移后验收必然触发 `UndefinedColumn`。首轮两项真实测试均失败，见 [postgres-first.log](postgres-first.log) 及 [environment-first.json](environment-first.json)。这项问题此前没有被 SQLite 或静态契约测试检出。

现改为读取 `pg_catalog.pg_class.relrowsecurity/relforcerowsecurity`，通过 `pg_namespace` 限定当前 schema 和普通/分区表；隔离 policy 也显式从 `pg_catalog` 查询。所有既有租户表仍须同时具备 ENABLE、FORCE 和 `oria_tenant_isolation`，没有删除或放宽验收。字段依据见官方 [pg_class](https://www.postgresql.org/docs/17/catalog-pg-class.html) 与 [pg_tables](https://www.postgresql.org/docs/17/view-pg-tables.html) 文档。

补充六项真实负例，对 Platform approvals / Business merchants 分别临时关闭 ENABLE、取消 FORCE 或删除 policy，均断言验收拒绝；DDL 仅在可销毁测试库事务内执行并回滚，回滚后再次确认验收通过。迁移文件、平台版本、依赖和鉴权规则均未更改。

另修正此前未实际运行的 Enterprise 审批 fixture：补上现有 Approval 契约要求的 `created_at/updated_at`。新增测试首轮的事务观测错误也已修正；没有改变业务模型来迁就测试。

## 结果与覆盖

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 最终真实 PostgreSQL | **8 passed，3.58 秒，无 skip** | [postgres-saver-expanded.log](postgres-saver-expanded.log)、[environment-saver-expanded.json](environment-saver-expanded.json) |
| 本地定向回归 | **27 passed / 4 warnings，6.46 秒** | [community-targeted.log](community-targeted.log) |
| 格式 / Lint / 类型 | 359 文件格式、Ruff、179 源文件 mypy 通过 | [lint.log](lint.log) |
| wheel / sdist | 离线构建通过，使用现有构建依赖缓存 | [build.log](build.log) |
| 发行物内容 | wheel 中 runner 与最终源码字节一致；wheel/sdist 不含缓存 | [package-content.log](package-content.log) |

真实卡包含：

1. 双 revision chain 实际升级至 `platform_0011 / business_0011`，并执行 schema/外键/RLS 验收。
2. 普通角色下保存与读取 Approval、跨 tenant 不可读；Business 商家数据按 tenant 分隔。无 tenant context 的原始 SQL 和仅传 `:tenant_id` 的 SQL 读不到行，可信 API 设置 context 后可读；连接池复用后的新事务清空 context。
3. 六项安全控制缺失负例全部拒绝，回滚后完整控制恢复。
4. 官方 `AsyncPostgresSaver.setup/aput/aput_writes/aget_tuple/alist` 实际访问 PostgreSQL；pending writes 内容一致，其他 tenant/namespace 不可读取，后继 checkpoint 的 parent 游标和外部 tenant 标识正确，列表返回已保存的两个 checkpoint。checkpoint 表由官方 saver 创建，不纳入 Oria Alembic 链。

本地定向覆盖配置/URL 脱敏、独立数据库、SQLite 两条迁移链、checkpoint adapter、审批卡片迁移及 API 只读启动门禁。四条 warning 为既有 SQLite/Alembic 外键反射提示。本轮修改仅涉及 PostgreSQL 验收查询和 Enterprise 测试，没有重跑无关全量套件；[10 月 16 日的 1094 项本地回归](../20261016-t02-receiver-lifecycle/summary.md)作为未变更模块的既有证据引用，不冒充本轮新结果，两轮数量不相加。

## 执行与历史

真实测试通过进程内生成的临时 DSN，显式设置 `ORIA_RUN_ENTERPRISE=1`、`ORIA_ENTERPRISE_TARGETS=postgres`；测试密码和 DSN 不在命令文本中。等价验证命令：

```bash
uv run pytest -m enterprise tests/integration/test_v06_postgres_enterprise.py -q -rs --tb=short
uv run pytest -q tests/contract/test_v06_postgres_persistence.py tests/contract/test_v06_checkpoint_adapter.py tests/integration/test_v03_execution_ledger_migration.py tests/integration/test_v06_approval_cards.py tests/integration/test_v06_api_lifecycle.py
make lint
uv build --out-dir .artifacts/postgres-local-20261017
```

Python 命令均关闭依赖下载和同步；本轮没有新的模型调用或真实飞书消息。发行物放在忽略目录 `.artifacts/postgres-local-20261017/`。只保存脱敏证据；日志仅替换 DSN/密码/用户目录并移除行尾空白，失败内容和计数保留。

| 轮次 | 结果 | 原因与证据 |
| --- | --- | --- |
| first | 2 failed，3.38 秒 | 实测目录查询缺陷；[日志](postgres-first.log) |
| catalog-fix | 7 failed / 1 passed，4.60 秒 | saver 已通过；旧 fixture 缺时间字段，六项新增负例在手动 rollback 后仍位于已关闭的事务上下文，属于测试观测错误；[日志](postgres-catalog-fix.log)、[环境](environment-catalog-fix.json) |
| verified | 8 passed，4.61 秒 | 修正 fixture 和负例事务；[日志](postgres-verified.log)、[环境](environment-verified.json) |
| saver-expanded | 8 passed，3.58 秒 | 为覆盖真实隔离和恢复契约，进一步补核 pending writes、tenant/namespace、parent 和列表；[日志](postgres-saver-expanded.log) |

第二轮环境 JSON 的原始 `attempt=final` 保留，该失败轮的文件改名为 catalog-fix 以免被误认为最终通过。不是无改动重复采样；每次复验均对应缺陷修复、测试修正或明确未覆盖契约。最初 Ruff 检查发现新测试一处长装饰器，定向格式化后完整静态检查通过。

## 状态与后续

- **已解除**：V0.6-T01 的真实 PostgreSQL 缺环境阻塞。[9 月 20 日 blocked 卡](../20260920-t01/summary.md)及本轮失败记录均保留，不改写历史结论。
- **未验证**：生产 PostgreSQL、TLS、迁移存量业务数据、PostgreSQL API、双 worker/lease/fencing、真实飞书、Live Provider、Enterprise 业务 Adapter 和 Performance。API 审批启动仍限定 SQLite，本卡不改变其配置限制。
- 飞书准备进展（同日后续）：用户已指定本地测试目录及 `oriaTest` 测试群；检查时目录为空，用户确认尚未创建应用。已在指定目录准备仅当前用户可读写的空凭证模板，并填好开发者后台的 `OriaTest` 创建表单，等待用户确认生成新应用凭证；应用尚未创建或授权。未读取飞书本地登录凭证，未发送真实消息，也未进行真实通道验收。客户端用于展示和点击测试卡片，真实接口仍需应用凭证及回调链路；群邀请二维码不作为 API 群 ID。
- 2026-10-18 后续：用户改为提供已有 ClawBot 应用凭证，并明确批准向 oriaTest 投递两条测试内容。文本和无按钮卡片均获真实成功回执，用户确认可见且显示正常；见独立的 [真实出站验证卡](../20261018-feishu-channel/summary.md)。没有创建上述待确认的新应用。该后续不改变本卡 PostgreSQL 结论，也不代表真实入站/审批回调或完整业务链路通过。
- T02 继续按卡片生产投递/更新、带理由拒绝、详情/翻页收口；Job/wait/Graph 恢复属于后续任务。V0.5-T07 Live 暂停状态不变。未提交、推送或触发远端 CI。
