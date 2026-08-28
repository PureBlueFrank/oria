---
run_id: "20260828T090353+0800"
version: "V0.1"
task_id: "V0.1-T07"
depends_on: ["V0.1-T04", "V0.1-T06"]
verification_level: "F"
base_commit: "91de703fed1b021e3c70740b62cb96629ad39bf9"
commit: null
executed_at: "2026-08-28T09:03:53+08:00"
provider_model: "脚本化 Fixture Provider；未调用真实 Provider"
embedding_model: "FixtureEmbedder 128 维；未下载或推理真实 BGE"
commands:
  - cmd: "make lint"
    result: "113 files formatted；Ruff All checks passed；mypy Success: no issues found in 73 source files"
  - cmd: "make test"
    result: "171 passed（not live / not enterprise / not performance）"
  - cmd: "make build"
    result: "sandbox 内首次因 PyPI DNS 受限失败；按权限流程联网重试后成功构建 wheel 与 sdist"
  - cmd: "make smoke"
    result: "oria 0.1.0"
  - cmd: "隔离 venv 安装 wheel 并运行 verify_t07_wheel.py"
    result: "Prompt package resource、research graph 拓扑与同 thread 跨租户 Checkpoint 隔离通过"
  - cmd: "python scripts/validate_scenario_a_dataset.py --allow-pending"
    result: "30 cases / 30 critical；status=pending_human_review"
assertions:
  - id: "V01-PROMPT-01"
    covered: true
    note: "PromptManager 仅按显式正整数版本读取 package resource；StrictUndefined、元数据变量与调用变量三者一致性受测。"
  - id: "V01-AGENT-01..06"
    covered: true
    note: "已覆盖规范化路由、整批预检、只读重试归属、canonical observation、32 KiB 转存、一次 finalization-only repair、无进展与 model/tool/token/cost/deadline 终止。"
  - id: "V01-AGENT-02 / 硬资格"
    covered: true
    note: "12 条全合成 Fixture 现确定产生 10 条合格候选；demo-m003/demo-m004 被硬排除，伪造输出不允许 repair 掩盖。"
  - id: "V01-GRAPH-01/02"
    covered: true
    note: "永久 StateGraph 仅有 model/tools/validate 节点；真实 Oria nodes + Fixture + InMemorySaver 完成两工具、10 商家、规则引用和 CampaignProposal E2E-F。"
  - id: "V01-CKPT-01"
    covered: true
    note: "官方 AsyncSqliteSaver 适配器实现 aput/aput_writes/aget_tuple/alist，关闭 pickle fallback；同 external thread 跨租户 get/list/resume 互不可见，对外 config 不泄漏 storage key。"
  - id: "Scenario A Golden 草案"
    covered: false
    note: "已建立 30 条 synthetic critical 候选、sha256 manifest 和自动完整性门禁；尚缺实际人工逐条审阅。"
result: "blocked"
blocked_by:
  - "Scenario A v1 的 30 条 Golden 仍为 pending_human_review；按项目规范，AI 检查不能代替真实人工审阅。"
known_limits:
  - "尚未创建 committed baseline，也未启用独立 eval-golden CI job；这是为避免违反‘首次 baseline 只能在人工审阅后创建’的明确规则。"
  - "验证等级仅为 F；未调用真实 DeepSeek，未加载真实 BGE，不构成 Community Real 或 Live 证据。"
  - "GitHub Actions 未在远端实际运行新增 T07 package step；本报告只记录本机 macOS 结果。"
  - "当前 T05 remediation 02、T06 与 T07 变更尚未提交，故 commit 保持 null。"
---

# V0.1-T07 验证报告

## 结论

T07 的 Prompt、CampaignProposal、有界 Agent loop、租户隔离 Checkpoint 和安装包验证均已通过 F 等级本地门禁。代码结果是 **passed**，但 T07 整体仍是 **blocked**：30 条 Golden 只是草案，尚未获得真实人工审阅，因此 baseline 与 `eval-golden` 不能合规创建。

## 主要交付

- 显式版本 PromptManager 与 `merchant_selection/v1` package resource。
- 本地 schema + 规则快照 + 商家候选 + citation 交叉校验的 CampaignProposal。
- 固定 `model -> tools -> validate` StateGraph，Context 通过 LangGraph runtime context 逐次传入。
- 整批工具预检、执行器重试、稳定 evidence fingerprint、大结果转存、一次修复和全量预算终止。
- 官方 AsyncSqliteSaver 的 tenant-safe 完整异步适配。
- 30 条 synthetic Golden 候选、manifest、审阅清单和 fail-closed 人工审阅门禁。

## 下一步

请人工逐条审阅 `eval/datasets/scenario_a/v1.jsonl`。审阅通过后需填写 reviewer/时间、重算 manifest，再执行确定性 harness 创建首个 baseline 并启用独立 `eval-golden` job。
