# ADR-004：Checkpoint 仅适配官方 async saver，并使用 tenant-qualified storage thread ID

- 状态：已接受
- 日期：2026-09-20
- 关联任务：V0.1-T07、V0.6-T01

## 背景

SQLite 和 PostgreSQL checkpoint 必须保留 LangGraph 安装版的 checkpoint namespace、parent、channel versions、versions seen、pending writes 和 metadata 语义。自行实现精简 saver 会分叉官方恢复契约。

## 决策

1. Community 委托官方 `AsyncSqliteSaver`，PostgreSQL 委托官方 `AsyncPostgresSaver`；Oria 不重写 saver 表结构、并发或恢复语义。官方 saver 表由 `setup()` 管理，不进入 Oria Alembic 链。
2. 单一 `TenantCheckpointSaver` 适配安装版完整 async 契约：`aput`、`aput_writes`、`aget_tuple`、`alist`，并为删除提供 tenant-aware 入口。
3. 外部 `thread_id` 不直接作为 saver key。storage key 固定为 `v1` + tenant/thread UTF-8 长度前缀 + base64url，返回给调用者的 config/tuple 只包含外部 ID。
4. adapter 在 metadata 增加 tenant 和 external thread ID 并在读取时核对，其余 checkpoint/metadata/pending-write 字段原样委托。
5. serializer 继续禁止 pickle fallback，只允许明确的 Oria msgpack 类型。

## 后果与验证

- 两种 backend 共用同一 tenant 编码和外部契约，同 external thread 在不同 tenant 下映射为完全不同的 storage key。
- 契约测试必须断言 namespace、parent、metadata、pending writes 与 `task_path` 不丢失；真实 PostgreSQL saver 需在显式 enterprise DSN 下单独验证。
