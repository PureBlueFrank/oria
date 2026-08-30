---
run_id: "20260830T171211+0800"
version: "V0.2"
task_id: "V0.2-T06"
verification_level: "F + L"
base_commit: "3333000b8430d8e39ef3e386e6ba61e1d748464e"
commit: null
worktree_state: "dirty；包含当前 V0.2-T05/T06 未提交变更"
executed_at: "2026-08-30T17:12:11+08:00"
provider_model:
  provider: "deepseek"
  model: "deepseek-v4-flash"
  api_dialect: "responses"
dataset_version: "rag/1"
result: "passed"
blocked_by: []
---

# V0.2-T06 DeepSeek 修复后 Live 验证卡

## 结论

T06 已通过。真实 Provider Nightly 在冻结 holdout 上完成 12/12 请求并通过硬预算闭环；修复后的 DeepSeek Responses Provider Live 完成文本、语义流式、工具调用和 401 错误映射四项检查。此前 `20260830T163443+0800` 失败卡保持不可变，作为兼容性回归历史。

## 根因与修复

独立合成诊断证明官方字符串输入和 Oria 消息列表输入在默认思考模式下均因显式 `tool_choice="required"` 返回 HTTP 400，安全错误摘要为 `Thinking mode does not support this tool_choice`，因此根因不是 Oria 消息映射。适配层现仅在 `DeepSeek + Responses + 显式 tool_choice` 时发送 `reasoning.effort="none"`；普通文本、流式及未显式选择工具的请求仍保留默认思考模式。

修复后二次诊断的两种输入均返回 HTTP 200，并各自产生一次 `oria_health_probe` 工具调用，结论为 `both_accepted`。

## Provider Live（passed）

| 检查 | 结果 |
| --- | --- |
| 文本 | passed |
| 语义流式 | passed |
| 工具调用 | passed |
| 401 错误映射 | passed |
| 请求 | 4 total / 3 successful / 3 request IDs |
| 模型 | `deepseek-v4-flash` |
| input/output | `462 / 96` tokens |
| reasoning/cache read | `61 / 256` tokens |
| 耗时 | `2101.61 ms` |

第四次请求使用刻意无效的合成凭证验证 401 映射，因此不应有成功 request ID 或 usage。

## Provider Nightly（passed，沿用同日不可变证据）

- 冻结 holdout：6 条 critical case × 2 次，共 12/12 请求完成。
- 模型：`deepseek-v4-flash`；input/output：`2446 / 465` tokens。
- 冻结 peak 价格估算成本：`$0.001090232`。
- 原始证据：[`../20260830T163443+0800/live-evidence.json`](../20260830T163443+0800/live-evidence.json)。

## 离线门禁与安全边界

- 修复相关 Provider/诊断契约：`83 passed`。
- 完整默认套件：`316 passed, 1 deselected`。
- Ruff 与 mypy：通过。
- Key 仅由用户在本机关闭回显注入，运行后 `unset`；未写入文件或报告。
- 证据只保存 request ID、模型、usage、检查状态、耗时和脱敏诊断结论，不保存提示词、模型输出、原始响应或凭证。
- 本卡只证明 DeepSeek adapter；其他 Provider 仍保持独立 `live_verified=false`，企业 Adapter 未实现、未验证。
