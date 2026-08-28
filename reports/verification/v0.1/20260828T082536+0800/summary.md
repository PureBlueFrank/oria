---
run_id: "20260828T082536+0800"
version: "V0.1"
task_id: "V0.1-T05-remediation-02+V0.1-T06"
depends_on: ["V0.1-T03", "V0.1-T04", "V0.1-T05"]
verification_level: "F"
base_commit: "91de703fed1b021e3c70740b62cb96629ad39bf9"
commit: null
executed_at: "2026-08-28T08:25:36+08:00"
environment: "macOS 26.6.1 x86_64 / Python 3.11.15 / uv 0.12.6 / uv.lock sha256:bc4b7cea4ab80d6c3733680ffca96027e6fa3327bea3f4137f983d3a186a6c4a / Chroma 1.5.9 / httpx 0.28.1 / jsonschema 4.26.0 / pydantic 2.13.4 / pytest 9.1.1 / mypy 1.20.2"
provider_model: "未调用 Provider；T06 仅执行确定性工具层"
embedding_model: "FixtureEmbedder 128 维；未下载或推理真实 BGE"
config_fingerprint: "runtime-specific; no secret persisted"
commands:
  - cmd: "make lint"
    result: "92 files formatted；Ruff All checks passed；mypy Success: no issues found in 61 source files"
  - cmd: "make test"
    result: "145 passed（not live / not enterprise / not performance）"
  - cmd: "make build"
    result: "Successfully built dist/oria-0.1.0.tar.gz 与 dist/oria-0.1.0-py3-none-any.whl"
  - cmd: "make smoke"
    result: "oria 0.1.0"
  - cmd: "隔离 venv 安装最终 wheel 并运行 verify_t02_wheel.py–verify_t06_wheel.py"
    result: "从 site-packages 导入 60 个 Oria 模块；T02/T03/T04/T05/T06 安装包验证全部通过"
assertions:
  - id: "T05 remediation / 删除可重试"
    covered: true
    note: "注入 Chroma 删除失败后 catalog 与 ObjectStore 保持可恢复；重试可删除原文并提交 catalog 删除。"
  - id: "T05 remediation / 多投影删除"
    covered: true
    note: "128 维与 32 维两个 projection 同时存在时，删除后两个 collection 均不再包含目标 chunk。"
  - id: "V01-TOOL-01 / 参数与快照"
    covered: true
    note: "intent/effective_at/rule_snapshot_id/limit strict schema 生效；未知字段、非法枚举、无时区时间、非法或未知 snapshot 均拒绝。"
  - id: "V01-TOOL-02 / 硬资格"
    covered: true
    note: "SQLite 12 条 fixture 经同一 EligibilityPolicy 得到 5 条候选与 7 条排除；limit=2/10 生效，排除原因以无 ID 汇总返回。"
  - id: "T06 / ToolRegistry"
    covered: true
    note: "allowlist 精确为 search_campaign_rules/query_merchants；启动后注册表与 ToolSpec schema 不可变；非 allowlist 和 Policy deny 均在执行前拒绝。"
  - id: "T06 / 模型可见边界"
    covered: true
    note: "规则结果含六类规则、snapshot version/hash 与逐字段有效 citation；工具结果携带 trust/provenance/classification。黑白名单字段/成员、排除商家 ID 与销售组织原文不进入 schema 或结果。"
  - id: "package 门禁"
    covered: true
    note: "CI package job 新增 verify_t06_wheel.py；本机最终 wheel 在隔离 venv 从 site-packages 执行两个工具与脱敏断言通过。"
result: "passed"
blocked_by: []
known_limits:
  - "验证等级仅为 F；未调用真实 DeepSeek，未加载真实 BGE，不构成 Community Real 或 Live 证据。"
  - "当前 12 条合成商家 fixture 有 5 条硬资格候选；V0.1-S1 要求最终展示 10 条候选，需在 T07/T08 的 golden/demo 冻结前补足并重新版本化数据证据。"
  - "V0.1 Core Gate 仍缺 T07–T09，Live 卡 T10 未运行。"
  - "GitHub Actions 未在远端实际运行新增 T06 package step；本报告只记录本机 macOS 结果。"
  - "当前 T05 remediation 02 与 T06 变更尚未提交，故 commit 保持 null。"
---

# V0.1-T05 remediation 02 + T06 验证报告

## 结论

T05 删除流程已改为可重试的全 projection 清理，并保留历史版本引用用于恢复半完成删除。T06 两个只读工具、专用 ToolRegistry、参数/结果 schema、统一授权、trust metadata、确定性资格过滤与敏感输入脱敏已完成 F 等级验证，判定为 **passed**。

本结论只覆盖本地 Fixture、真实 SQLite/ObjectStore/Chroma 和安装包契约，不包含真实 DeepSeek/BGE，也不代表 V0.1 Core 或 Live Gate 已通过。

## 主要交付

- `search_campaign_rules` 返回 tenant-qualified snapshot、六类脱敏规则与模型可见字段 citation；缺失或冲突规则显式返回 `unresolved_items`。
- `query_merchants` 只接受已缓存且完整性/引用仍有效的 snapshot ID，硬资格由 EligibilityPolicy 确定性执行。
- ToolRegistry 对 allowlist、输入 schema、PolicyDecision、超时和成功结果 schema 逐层校验，启动后不可变。
- 商家结果只包含候选和排除 reason count，不包含被排除 ID、黑白名单成员或销售组织原文。
- CI 安装包门禁扩展到 T06。

## 下一步

进入 V0.1-T07，实现 version 必填的 PromptManager、`merchant_selection/v1`、`CampaignProposal`、永久 `research_agent` StateGraph 与至少 30 条人工审阅 golden。执行 S1 前必须解决 10 条硬资格候选 fixture 的数量门禁。
