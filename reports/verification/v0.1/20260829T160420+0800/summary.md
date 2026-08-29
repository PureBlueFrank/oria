---
run_id: "20260829T160420+0800"
version: "V0.1"
task_id: "V0.1-AUDIT-REMEDIATION"
depends_on: ["V0.1-T10"]
verification_level: "C + L"
base_commit: "f6d052c0834ddf7addc254eee95387cc20e75254"
commit: null
worktree_state: "dirty；覆盖当前未提交的 V0.1 变更"
executed_at: "2026-08-29T16:04:20+08:00"
provider_model:
  provider: "deepseek"
  model: "deepseek-v4-flash"
embedding_model:
  model: "BAAI/bge-small-zh-v1.5"
  revision: "a7ec18349c42fc774b0e86af26215e38a10fbe9d"
config_fingerprint: "sha256:3edd7613d7286fa66669b9b1ddf0011e9f108cdb3d95738cfd18bde216e48ea9"
dataset_version: "scenario_a/1"
commands:
  - cmd: "uv run pytest（定向回归）"
    exit_code: 0
  - cmd: "uv run pytest -m 'not live and not enterprise and not performance' -q"
    exit_code: 0
  - cmd: "uv run python scripts/run_scenario_a_golden.py"
    exit_code: 0
  - cmd: "ORIA_RUN_LIVE=1 ORIA_LIVE_TARGETS=deepseek uv run pytest -m live -q"
    exit_code: 0
  - cmd: "make lint && make build && make smoke"
    exit_code: 0
result: "passed"
blocked_by: []
known_limits:
  - "total_cost 仍未配置冻结价格快照；只保存完整 token usage。"
  - "远端 GitHub Actions 尚未在当前未提交 worktree 上运行。"
---

# V0.1 Agent 审计缺陷修复验证卡

## 结论

上一轮审计确认的四类缺陷已修复并通过本地与真实 DeepSeek 回归：失败结构化响应不再丢失 request ID/usage；用户输入不再进入 system message；`max_candidates` 同时进入模型可见 Tool Schema、执行端预检和最终提案校验；Demo 以 Agent 执行前后业务库完整指纹判断副作用。

## 真实验证过程

首次回归如实失败：DeepSeek 请求 `query_merchants(limit=100)`，执行端在任何商家查询前以 `policy_or_contract_violation` 拒绝，业务库未变化。随后加入按运行参数动态收窄的 Tool JSON Schema，并保留执行端硬校验；全新数据目录双跑通过。

| 项目 | 第一次成功运行 | 第二次成功运行 |
| --- | ---: | ---: |
| 模型轮次 / 已记录 request ID | 4 / 4 | 3 / 3 |
| input/output tokens | 18,314 / 3,333 | 12,630 / 1,017 |
| 只读工具调用 | 2 | 2 |
| 合格候选 / 推荐商家 | 10 / 10 | 10 / 10 |
| 可回查引用 | 52 | 52 |
| 业务库指纹变化 | 无 | 无 |
| 初始化新增商家 | 12 | 0 |
| 知识摄取幂等 | false | true |

第一轮成功运行发生一次允许的 finalization repair，但所有已计费响应均有 request ID 和 usage；第二轮为标准三轮。原 T10 中“失败响应 usage/request ID 未穿过错误契约”的已知限制已经关闭。

## 门禁结果

- 定向修复回归：43 passed。
- Core：188 passed，1 deselected。
- Golden：30/30，五项指标均为 1.0。
- Live：1 passed，188 deselected；测试内部执行两次真实流程。
- Ruff format/check、mypy、wheel/sdist、CLI smoke：通过。
- 新 wheel 在独立虚拟环境中执行 T07/T08 验证；T08 源码外双跑通过。

机器可读脱敏证据见 `live-runs.json`。Key 仅由 macOS Keychain 临时注入测试进程，未写入报告或仓库。
