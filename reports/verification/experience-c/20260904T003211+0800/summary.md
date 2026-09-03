# 体验优化任务 C 验证报告

```yaml
run_id: "20260904T003211+0800"
version: "五项体验优化"
task_id: "experience-c"
depends_on: ["V0.3-Core", "experience-b"]
verification_level: "F + C"
commit: "1f21db9"
executed_at: "2026-09-04T00:32:11+08:00"
environment: "macOS / Python 3.11.15 / uv 0.12.6 / uv.lock sha256:f1683586845697ed1095d4266a6656f32fe8f5c41e3637101975bd4716c55075"
provider_model: "mock / mock-demo"
config_fingerprint: "sha256:3501687c059c7c4e0ec2e505b3023014b07a7fb6dceec05eefa5e4c89f28074f"
dataset_version: null
eval_fingerprint: null
result: "passed"
blocked_by: []
known_limits:
  - "意图解析仅为确定性短语与关键词路由，不使用 LLM。"
  - "本轮未运行 Live、Enterprise 或 Performance。"
  - "真实券、招商、商品库、选品、C 端投放和 IM Adapter 未验证。"
```

## 验证命令与实际结果

- 任务 B 前置基线：`uv run pytest tests/unit/test_cli.py tests/unit/test_workflow_presentation.py -q`，`18 passed`。
- 指定验收：`uv run pytest tests/unit/test_chat_router.py tests/unit/test_cli.py tests/unit/test_workflow_presentation.py -q`，`37 passed`。
- Chat/权限相关定向与 Scenario A 集成：共 `43 passed`。
- 静态门禁：`make lint`；Ruff format 检查 `256 files already formatted`，Ruff Lint 通过，mypy 检查 `135` 个源码模块通过。
- 完整本地套件：`make test`，`648 passed, 1 deselected, 4 warnings`，耗时 `166.68s`。4 条均为既有 SQLite migration `SAWarning`。
- CLI smoke：真实执行 `oria --help`、裸 `oria </dev/null`、管道输入 `oria chat` 和既有 `oria config doctor --output json`；均正常退出，非 TTY 未进入阻塞输入。

## 可观察断言

- `IntentRouter` 支持发起活动、查询状态、平台审批决定、业务确认决定以及 `/help`、`/quit`、`/new`；类目、城市/区域、报名模式和目标数量缺失时逐项追问。
- 完整 `ScenarioAWorkflowRequest` 从 `default_request()` 派生，只覆盖用户可见槽位，不由聊天层生成内部幂等键或过期时间。
- `/status` 和 `/switch` 从 tenant-qualified checkpoint 读取快照，再复用 `WorkflowViewModel + render_workflow`。
- 裸 `oria` 仅在 stdin/stdout 同时为 TTY 时进入 chat；非 TTY 输出帮助。`oria chat` 显式进入同一循环。
- Chat 审批固定以 `local-campaign-admin (campaign_admin)` 调用现有 `ApprovalService`。真实 SQLite 集成断言该主体得到 `approval operation is not authorized`；chat 仅提示所需的 `launch_approver` 或 `consumer_publish_approver` 和独立审批命令，没有切换角色或构造审批人。
- Ctrl-C 只捕获当前 `input()` 的中断，文案明确说明已提交操作不会撤销。

## 真实 Chat 输出摘录

以下摘录来自同一 `.artifacts/experience-c-dialogue` Community 数据目录；LLM/Embedding/外部 Adapter 分别是 Mock、Fixture 和 Mock，Checkpoint 与业务状态使用真实本地 SQLite。

```text
可信本地主体: local-campaign-admin (campaign_admin)
oria> 发起华东餐饮招商活动, 报名模式 hybrid, 目标 10 家
招商活动自动化 · 第 2/10 阶段
当前阶段: 招商发布审批
规则、候选范围及活动/券草案已冻结, 等待招商发布审批。
下一步命令: oria approval approve --thread-id chat-thread-390f... --approval-id approval_3dc9...

oria> 批准
审批被拒绝: 当前可信本地主体 local-campaign-admin (campaign_admin) 不具备 launch_approver 角色, chat 不会切换身份或冒充审批人。
请由独立可信审批身份执行: oria approval approve --thread-id chat-thread-390f... --approval-id approval_3dc9...

# 独立可信审批命令通过后，注入 Mock 报名并关窗，再重新进入 chat：
oria> /switch chat-thread-390f...
招商活动自动化 · 第 5/10 阶段
当前阶段: 动态业务确认
当前第 1/3 级, 由商家确认, 下一位为销售。

oria> 确认
招商活动自动化 · 第 5/10 阶段
当前阶段: 动态业务确认
当前第 2/3 级, 由销售确认, 下一位为销售经理。
```

实际输出的每次工作流状态均包含规则摘要、商家候选和十步流程表；摘录省略重复表格行以控制报告长度。

## 未验证项

- 未调用真实公开模型，不能把本报告写成 Live Provider 结果。
- 未连接真实企业 Adapter，不能声称企业审批身份、外部招商或投放接入已验证。
- 裸命令的 TTY 分支由单元测试模拟 stdin/stdout TTY；显式 `oria chat` 已用真实管道输入执行。
