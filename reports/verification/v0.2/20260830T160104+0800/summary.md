---
run_id: "20260830T160104+0800"
version: "V0.2"
task_id: "V0.2-T06"
verification_level: "F；Live blocked"
base_commit: "3333000b8430d8e39ef3e386e6ba61e1d748464e"
commit: null
worktree_state: "dirty；包含当前 V0.2-T05/T06 未提交变更"
executed_at: "2026-08-30T16:01:04+08:00"
provider_model:
  provider: "deepseek"
  model: "deepseek-v4-flash"
dataset_version: "rag/1"
result: "blocked"
request_count: 0
blocked_by:
  - "当前进程没有 DEEPSEEK_API_KEY"
  - "GitHub 仓库 Actions Secret 列表为空，且未配置 GitHub Environment"
  - "macOS Keychain 按 DEEPSEEK_API_KEY service/account 均未找到条目"
---

# V0.2-T06 Provider Live / Nightly 验证卡

## 结论

T06 的 Provider Live smoke 与真实 Nightly 请求循环已实现并通过离线门禁，但真实 DeepSeek 请求因当前可用作用域内没有 `DEEPSEEK_API_KEY` 而阻断。所有凭证检查均发生在 Provider 初始化之前，本轮实际请求数为 0；不得把本卡声明为 Live 通过。

## 已实现

- `run_provider_live.py` 使用显式 target 验证文本、语义流式、工具调用和真实 401 错误映射；证据只保存 request ID、provider model、usage、检查状态和耗时。
- `run_eval_nightly.py` 固定运行冻结 holdout 的 6 条 critical case × 2 次，共 12 个请求；每个请求前预留输入、输出、成本、case 和墙钟预算，响应后只按 Provider usage 结算。
- 不完整样本、模型不匹配、usage 超出预留或预算耗尽均 fail closed，不会标记 `passed`。
- reasoning token 按 output token 子集处理，避免在账本中重复计费。
- `eval-nightly.yml` 顺序固定为零请求 preflight → Provider Live smoke → bounded Nightly，并始终上传脱敏 JSON 产物。

## 验证结果

| 项目 | 结果 |
| --- | --- |
| 定向 Nightly/workflow 契约 | `12 passed` |
| 完整默认测试 | `313 passed, 1 deselected` |
| Ruff format/check | 通过 |
| mypy | 通过，86 个源码文件 |
| wheel/sdist 构建 | 通过 |
| 安装态 wheel 资源与 Nightly API | 通过 |
| 本地 preflight | `blocked`，`request_count=0` |
| 本地 Provider Live / Nightly | `blocked`，`request_count=0` |

## Secret 定位证据

- `gh secret list --repo PureBlueFrank/oria` 成功返回，但没有 Secret 名称。
- GitHub 仓库 Environment 列表为空。
- `security find-generic-password -s DEEPSEEK_API_KEY` 与按 account 查询均返回 item not found；未读取或输出任何密码值。

## 下一步

将 `DEEPSEEK_API_KEY` 配置为 `PureBlueFrank/oria` 的 GitHub Actions repository secret，或提供现有密钥存储条目的准确名称。之后提交并推送当前 workflow，手动 dispatch `Eval Nightly` 的 `deepseek` target，下载并复核 `provider-live.json`、`nightly-run.json` 后才能把 T06 和 DeepSeek `live_verified` 标记为通过。
