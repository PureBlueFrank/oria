---
run_id: "20260829T211203+0800"
version: "V0.2"
task_id: "V0.2-T02"
depends_on: ["V0.1-Core", "V0.1-T02", "V0.2-T01"]
verification_level: "Fixture/Community CT + SEC"
base_commit: "ba7b79c69d31f82bfbb094b0ba84358dba4063a9"
commit: null
worktree_state: "dirty；仅含 V0.2-T02 阶段 D 测试、路线状态与本报告"
executed_at: "2026-08-29T21:12:03+08:00"
network_executed: false
live_verified: false
enterprise_verified: false
commands:
  - cmd: 'uv run pytest -m "unit or contract" -q'
    exit_code: 0
    result: "194 passed, 77 deselected in 6.44s"
  - cmd: 'uv run pytest -m "not live and not enterprise and not performance" -q'
    exit_code: 0
    result: "270 passed, 1 deselected in 47.43s"
  - cmd: "uv run ruff check . && uv run ruff format --check ."
    exit_code: 0
    result: "All checks passed; 130 files already formatted"
  - cmd: "uv run mypy src/oria"
    exit_code: 0
    result: "Success: no issues found in 79 source files"
  - cmd: "uv run pytest -m security -q"
    exit_code: 0
    result: "47 passed, 224 deselected in 6.55s"
  - cmd: "uv run python scripts/run_scenario_a_golden.py"
    exit_code: 0
    result: "30/30 passed；五项冻结指标均为 1.0"
result: "passed"
blocked_by: []
known_limits:
  - "未运行真实网络、Provider Live、Enterprise 或 Performance；不得据此声明相关能力已验证。"
  - "read_policy 与 outbox 本任务只建立 schema；企业策略加载、outbox dispatcher 和防篡改增强不在 T02 范围。"
  - "production fail-closed 使用本地 SQLite 审计表缺失故障注入验证，未连接真实生产审计后端。"
  - "V0.2-T03 的 owner/ACL/classification 生命周期、更新删除传播与 catalog→vector 重建增强尚未执行。"
---

# V0.2-T02 Platform ACL/Audit 验证卡

## 结论

V0.2-T02 的本地 Fixture/Community 门禁通过。`platform_0003` 可从空库升级并回滚到 `platform_0002`；PolicyEngine 是 document ACLFilter 的唯一来源，Retriever 不能接受调用方覆盖 tenant、subject/role 或 classification；拒绝决策写入 `audit_events`，敏感 payload 字段不会落入数据库；正式版 restricted 审计写入失败会中止操作。

## 已验证边界

- Policy：缺失 actor/executor、Context 主体不一致、未知 action、跨 tenant 均默认拒绝，拒绝决策不携带 ACLFilter。
- ACL：允许的 document/rule read 决策携带不可变 ACLFilter；Chroma pre-filter 与 catalog post-filter 使用同一 tenant、subject/role、classification 投影；无 classification 的 ACLFilter 返回空结果。
- Filter：调用方传入 `tenant_id/acl/allowed_subject_ids/allowed_roles/classification` 时拒绝，合并后的策略字段不可修改或清空。
- Audit：拒绝事件保存 actor/action/resource/decision/policy_version/args_hash/result/correlation；API key、完整 prompt、隐式思维链及合成 PII 字段值被替换或仅参与哈希，不保存原文。
- Migration：空 SQLite 从 base 升级到 `platform_0003` 后存在 `read_policy/audit_events/outbox`，回滚至 `platform_0002` 后三表与索引移除，既有知识表保留。

## 证据口径

全部测试使用本地 SQLite、Chroma 与合成 fixture，未读取真实密钥、真实客户数据或外部网络。Golden 输出位于 gitignore 的 `.artifacts/eval/scenario_a_v1.json`，未作为新的持久化能力卡提交。V0.2 总版本仍为“进行中”，不能由本卡推导为 V0.2 Core 或 Live 已完成。
