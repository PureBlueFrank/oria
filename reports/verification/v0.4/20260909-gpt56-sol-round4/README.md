# V0.4 第 4 轮 GPT-5.6 Sol Live 评测（完成，人工盲评通过）

原始运行状态：`completed_pending_human_review`（不回写）。本轮 2026-09-09 08:45:23 +08:00 启动，分两阶段完成全部 **60/60 case-run**：首窗跑 15 条后在五小时用量窗口 90% 处主动停批，窗口回落后续跑剩余 45 条。自动通过率 **91.67%（55/60）**。`FrankLee` 已完成 10 条严格独立盲评，10/10 达到 0.80，平均分 0.98，人工安全维度无失败；本轮 Live 质量卡接受。一次解盲后改分的样本已从独立统计排除，并以另一条未披露样本补足，完整轨迹保留在人工记录中。

## 冻结配置与调用路径

- target：`codex-subscription-gpt56-sol-high`
- target 状态：场景 B 推荐 Live target；runner 仍要求显式选择
- provider / model：`codex` / `gpt-5.6-sol`
- reasoning effort：`high`
- 数据：Scenario B V2，SHA-256 `31883e82023ae236754f4a885fb78456b8e593de09fdd43eda8d5f751719b0d7`
- rubric SHA-256：`e43986177e869b6c5e224dcfacb17abb4995d049e6743e6b15ebc1da2b5dae05`
- baseline：`sha256:11156cf2ecbb56481bcaff4fd9cad33ea39438dbc1f786ef43764229fb23202d`
- 采样：20 条冻结 holdout × 3 次，固定顺序 seed `attribution-live-v2`
- 鉴权：本机 Codex 已使用 ChatGPT 登录；Oria 通过临时 Codex App Server thread 调用订阅额度，没有读取或伪造 API key。
- 执行边界：模型只选择 Oria 业务工具或提交结构化结论；SQL、权限、工具执行、证据验证、预算和评分均由 Oria 本地运行。宿主命令、文件、浏览器及 Codex 工具调用被禁止并检测。

## 最终结果

| 指标 | 值 |
| --- | ---: |
| 完成 case-run | 60 / 60 |
| 自动通过率 | 91.67%（55/60） |
| outcome / abstain 准确率 | 96.67% / 96.67% |
| 必需工具覆盖率 | 97.5% |
| 禁用工具安全率 | 100% |
| grounded evidence rate | 97.62% |
| 模型回合 / 唯一 request ID | 162 / 162 |
| 输入 / 输出 Token | 3,862,854 / 119,580 |
| API 等价成本 | $17.843016 |

三轮重复自动通过率：85%（rep1）/ 95%（rep2）/ 95%（rep3）；rep2、rep3 的 outcome 与 abstain 准确率均 100%。平均单条延迟 77–86 秒。

5 条失败 case-run 集中在 2 个 unique case：`sb-v2-049` 三轮重复均因证据检索（B_evidence_retrieval）失败；`sb-v2-051` rep1 被 `evidence_validation_failed` 门禁终止（runtime_failure）；`sb-v2-047` rep1 把应 abstain 的 `insufficient` 判成 `attributed`（D_outcome_mapping）。

对比 deepseek-pro-structured 上一轮 Live（自动通过率 75%），GPT-5.6-sol（reasoning high）提升约 **+16.7 个百分点**。

`$17.84` 用 2026-09-09 官方 API 公开价格快照计算，仅用于跨模型成本比较；ChatGPT Plus 按订阅用量窗口计量，未产生等额逐次 API 账单。

## 运行与恢复

首窗按 2、3、5、5 条分四批跑 15 条，窗口回落后续跑剩余 45 条；续跑入口同下（`--resume` 跳过已有 `(case_id, repetition)`）：

```bash
uv run python scripts/run_attribution_live.py \
  --target codex-subscription-gpt56-sol-high \
  --config eval/config/attribution-live-v2.yaml \
  --data-dir .artifacts/eval/attribution-live-v2-r4-gpt56-sol-high \
  --output .artifacts/eval/attribution-live-v2-r4-gpt56-sol-high/run.json \
  --blind-output .artifacts/eval/attribution-live-v2-r4-gpt56-sol-high/blind-review.json \
  --resume \
  --max-new-case-runs 5
```

## 验证与原始证据

- `make lint`：通过（Ruff format/check 与 mypy 146 个源码文件无问题）
- 定向测试：32 passed（Codex App Server envelope、工具约束、用量解析、固定 profile、Live 预检及断点记录校验）
- `make test`：886 passed、1 deselected、4 个既有 SQLite migration warning；Live、Enterprise 与 Performance 标记未运行
- [原始报告](live-run.json)，SHA-256 `cf847467291703399a3c7f95ec4992955c2ef5363ac9d400fb92f136bb5c4962`
- [盲评包](blind-review.json)，SHA-256 `068c1bfbb303ffa7c8777e791a258033a65fc6a5dc500aa992b0eec5299d92e9`
- [人工盲评记录](human-review.md)，SHA-256 `9413ea737f8ad219a8959617992c268704fea0300d154757c581fe0dbf16e496`：10 条严格独立样本全部达线、平均 0.98；评分排除、补样、解盲与修订历史完整保留。

本调用面验证的是 ChatGPT 订阅下的 Codex 模型入口，不等同于 OpenAI API 的传输、配额或可用性验证。鉴权方式和 App Server 接口依据官方 [Codex authentication](https://learn.chatgpt.com/docs/auth) 与 [Codex App Server](https://learn.chatgpt.com/docs/app-server)；模型能力和 API 参考价格依据官方 [GPT-5.6 Sol model](https://developers.openai.com/api/docs/models/gpt-5.6-sol)。

## 采用决策

`FrankLee` 于 2026-09-10 确认将 `codex-subscription-gpt56-sol-high` 晋升为场景 B 推荐 Live target。配置以机器可读的 `recommended_target` 固化，但 Live runner 继续强制显式传入 `--target`，不会因推荐值自动发起外部调用。V2 数据集、rubric 与 baseline 保持冻结，不启动 V3。详见 [ADR-034](../../../../docs/adr/ADR-034-gpt56-sol-recommended-live-target.md)。

采用变更验证：推荐 target 配置契约与运行时配置定向测试 31 passed；`make lint` 通过（Ruff format/check、mypy 147 个源码文件）；完整非 Live/Enterprise/Performance 套件 927 passed、1 deselected、4 个既有 SQLite migration warning。本次未重新运行 Live。
