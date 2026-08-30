---
run_id: "20260830T163443+0800"
version: "V0.2"
task_id: "V0.2-T06"
verification_level: "F + L"
base_commit: "3333000b8430d8e39ef3e386e6ba61e1d748464e"
commit: null
worktree_state: "dirty；包含当前 V0.2-T05/T06 未提交变更"
executed_at: "2026-08-30T16:34:09+08:00"
provider_model:
  provider: "deepseek"
  model: "deepseek-v4-flash"
  api_dialect: "responses"
dataset_version: "rag/1"
result: "failed"
blocked_by: []
---

# V0.2-T06 本机真实 DeepSeek 验证卡

## 结论

T06 未通过。真实 Provider Nightly 在冻结 holdout 上完成 12/12 请求并通过硬预算闭环；DeepSeek Provider Live 的文本与语义流式通过，但工具调用连续三次收到真实 HTTP 400，因此错误映射 smoke 未继续执行，`live_verified` 保持 `false`。

## 真实 Nightly（passed）

| 项目 | 结果 |
| --- | --- |
| 样本 | 6 条 holdout critical × 2 次 |
| 请求 | 12 expected / 12 sent / 12 completed |
| 模型 | `deepseek-v4-flash`，12/12 一致 |
| input/output | `2446 / 465` tokens |
| 估算成本 | `$0.001090232`，冻结 peak 价格 |
| 延迟 | min `671.35 ms`；p50 `879.65 ms`；p95/max `1188.49 ms` |

12 个 request ID 与聚合 usage 已保存于同目录 `live-evidence.json`。运行只保存 case ID、request ID、模型、usage、成本和延迟，不保存问题、提示词或模型输出。

## Provider Live（failed）

第三次最终 run：

- 文本：通过，真实 request ID/usage 已记录。
- 语义流式：通过，真实 request ID/usage 已记录。
- 工具调用：失败，HTTP 400 映射为 `InvalidRequestError`。
- 401 错误映射：未执行；工具 smoke 失败后 fail closed。

工具调用三次分别使用：命名 function choice、单工具 `required`、单工具 `required` + 官方示例式空参数 schema；三次均在第三个真实请求返回 400。官方文档声明 Responses API 支持 function tools 和上述 `tool_choice`，因此当前证据记录为 DeepSeek Responses 实际兼容性偏差，不通过切换 Chat Completions 或 Mock 绕过既定 profile。

## 离线门禁

- 完整默认套件：`313 passed, 1 deselected`（证据与状态卡收口后复跑）。
- T06 定向 Provider/Nightly/workflow 契约：`92 passed`；最后一次工具 smoke 调整后相关 Provider/workflow 契约：`82 passed`。
- Ruff、mypy、wheel/sdist 与安装态 wheel 验证通过。

## 安全与剩余边界

- Key 仅由用户在本机终端以关闭回显方式注入子进程，随后 `unset`；未上传 GitHub、未写入文件或报告。
- Provider 400 响应正文按安全错误边界未保存，避免持久化可能包含的上游请求内容。
- 本卡不证明完整 DeepSeek adapter Live 支持；Provider 状态保持 `live_verified=false`。
- 后续若 DeepSeek 修复 Responses 工具调用兼容性或能提供可复现的安全错误代码，再新增验证卡；不改写本卡。
