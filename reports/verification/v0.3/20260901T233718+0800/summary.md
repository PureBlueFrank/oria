# V0.3-T07 Scenario A Workflow 验证报告

- 任务：V0.3-T07 续跑第 2 轮，完成 reducer 冲突测试、10 步 Graph、受信 CLI 与分层验证
- 时间：2026-09-01 23:37:18 +08:00
- 验证层级：Fixture/Community，本地 SQLite、Mock Adapter 与合成数据
- 续跑基线提交：`8474b54`
- 本轮分阶段提交：`fd7ae06`、`a766288`、`9df094e`；测试收口为本报告所在提交
- 当前迁移头：`platform_0007` / `business_0009`
- 工具链：`uv 0.12.6` + `Python 3.11.15` + `uv.lock`

## 交付结果

- A：新增 22 个 reducer 用例，覆盖冲突 key、语义等价幂等、结合律、输入不变性、非有限数与无时区 datetime；同时修正 BaseModel 内 Decimal/datetime 过早 JSON 化的真实缺陷。
- B：实现 10 步 Scenario A Graph、两个独立 HITL、报名并行 join、动态确认链、selection external wait，checkpoint 为唯一恢复真相源；Graph node 不接触 Repository 或 SQL。
- C：增加 workflow start/resume、approval approve/reject 与 4 个 Mock 事件命令；CLI 不暴露 tenant/roles/subject/actor 冒充选项，每次命令使用固定受信本地身份并重建 Runtime。
- D：InMemorySaver Graph-UT 覆盖批准、拒绝、过期、approval/args/checkpoint/policy 篡改；AsyncSqliteSaver E2E 跨多个进程作用域 Runtime 恢复，完成双来源唯一键汇聚、三级确认、inbox 去重/脱敏、两个审批与最终投放通知。

## 验证命令与真实结果

```text
$ make lint
239 files already formatted
All checks passed!
Success: no issues found in 125 source files

$ make test
593 passed, 1 deselected, 4 warnings in 117.04s

$ uv run pytest -q -m security
99 passed, 495 deselected in 22.99s

$ uv run pytest -q tests/unit/test_t07_scenario_a_graph.py \
    tests/integration/test_v03_scenario_a_workflow.py \
    tests/unit/test_cli.py tests/security/test_runtime_boundaries.py
16 passed in 9.20s
```

Community 套件的 4 条 warning 是既有 SQLite/Alembic downgrade 反射复合外键时的 `SAWarning`；对应 migration 测试均通过。E2E 中 13 条 tool execution 全部 `succeeded` 且每个业务幂等键计数为 1；Business audit 13 条、Business outbox 14 条、Platform outbox 4 条，均无重复 event ID。

## 未验证边界

- 未运行 Live、Enterprise、Performance、真实网络或企业 Adapter；本报告不声明真实券/商家侧/选品/C 端/IM 接入通过。
- T08 要求的完整五类杀进程故障注入仍未执行；本轮仅与已有 T04–T06 recovery 套件合并验证 T07 所需 S2–S6 核心断言。
- 未运行 `make build`、`make smoke` 或 T09 DeepSeek Live 卡，不声明这些项通过。
