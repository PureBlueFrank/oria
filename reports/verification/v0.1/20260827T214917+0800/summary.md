---
run_id: "20260827T214917+0800"
version: "V0.1"
task_id: "V0.1-T03-remediation-01"
depends_on: ["V0.1-T03"]
verification_level: "F"
base_commit: "1b67b431e3ccf7def5400179a346c9ad816443b9"
commit: null
executed_at: "2026-08-27T21:49:17+08:00"
environment: "macOS / Python 3.11.15 / uv 0.12.6 / uv.lock sha256:14df51cee897e58e68b5608e245bd1a761131acc30dba0ae50a8eb19f1ef17ae"
provider_model: null
commands:
  - cmd: "四条定向回归用例（修复前）"
    result: "4 failed；分别复现跨 tenant Repository 记录泄漏、lookalike schema 绕过、SQLite foreign_keys=0、Alembic OperationalError 未归一化"
  - cmd: "四条定向回归用例（修复后）"
    result: "4 passed"
  - cmd: "make lint"
    result: "60 files already formatted；Ruff All checks passed；mypy Success: no issues found in 41 source files"
  - cmd: "make test"
    result: "86 passed"
  - cmd: "make build"
    result: "Successfully built wheel 与 sdist"
  - cmd: "make smoke"
    result: "oria 0.1.0"
  - cmd: ".venv/bin/python scripts/verify_t03_bypass_boundaries.py --work-dir <fresh-tmp>/work"
    result: "rejected storage exposure, hard-rule overrides, restricted-field serialization, migration detours, path escapes, and asset tampering"
  - cmd: "临时 uv venv 离线安装 wheel 后运行 scripts/verify_t03_wheel.py"
    result: "verified installed T03 wheel assets, two revisions, idempotent data init, and zero Campaign/CouponBatch tables"
assertions:
  - id: "Repository seam 跨 tenant fail closed"
    covered: true
    note: "DefaultMerchantService 同时复核规则 tenant 与每条 MerchantRecord tenant；替换 Repository 返回其他 tenant 记录时只返回统一拒绝错误，不返回商家数据。"
  - id: "已 stamp head 的 lookalike schema 拒绝"
    covered: true
    note: "runner 校验列名、SQLite 声明类型、nullable、复合主键顺序与复合外键；仅伪造表名和 head 不能跳过 migration。"
  - id: "SQLite 外键实际启用"
    covered: true
    note: "platform/business async engine 每次 connect 执行 PRAGMA foreign_keys=ON；应用连接观测值为 1，孤儿 document_version 写入触发 IntegrityError。"
  - id: "Migration 异常安全归一化"
    covered: true
    note: "Alembic 抛出的 SQLAlchemyError 转换为 MigrationError/DataInitializationError；JSON CLI 返回稳定 data_init_failed，不输出 traceback 或建表 SQL。"
result: "passed"
blocked_by: []
known_limits:
  - "本轮仍仅为 Fixture/本地 SQLite 验证，未调用真实 Provider、Embedding 或企业系统。"
  - "当前修复尚未提交；base_commit 仅标识修复开始时的 T03 本地提交，不代表该 commit 已包含 remediation。"
  - "V0.1 Core Gate 仍缺 T04–T09，Live 卡仍未运行。"
---

# V0.1-T03 Remediation 01 验证报告

## 结论

针对代码审查发现的两个 P1 与两个 P2 问题已完成修复，四条用例均先在旧实现上复现失败，再在修复后通过。完整本地门禁为 **86 passed**，静态检查、构建、CLI smoke 和已安装 wheel 验证均通过，因此 T03 remediation 按 F 等级判定为 **passed**。

## 修复范围

1. Domain Service 不再把 Repository 的 tenant 隔离视为不可复核假设。
2. Migration runner 不再以「表名存在 + head 相同」代替 schema 完整性验证。
3. 应用 SQLite 连接强制开启外键约束。
4. Alembic/SQLAlchemy 异常统一进入脱敏 CLI 错误边界。

原始 T03 报告保留为历史证据，不回写其当时的命令与结论；本报告追加纠正其未覆盖的四条绕道。
