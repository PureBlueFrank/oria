# ADR-034：GPT-5.6 Sol 作为场景 B 推荐 Live target

- 状态：已接受
- 日期：2026-09-10
- 决策者：FrankLee
- 关联任务：V0.4-T05

## 背景

场景 B 原冻结 DeepSeek Live 卡完整执行但质量失败；后续 `deepseek-pro-structured` 三轮自动通过率约 75%，未完成人工验收。GPT-5.6 Sol 在同一冻结 V2 holdout、rubric、baseline 与三轮预算下完成 60/60，自动通过 55/60，并通过 10 条严格独立人工盲评。需要明确后续场景 B Live 验证的推荐目标，同时避免把推荐误解为无提示调用、默认业务模型或历史证据改写。

## 候选方案

1. 继续推荐 `deepseek-pro-structured`：调用路径既有，但质量约 75% 且未通过严格人工盲评。
2. 把 GPT-5.6 Sol 设为全局运行时默认：使用方便，但可能在 Demo、开发或 CI 中意外消耗订阅额度，并扩大本次验证结论。
3. 把 GPT-5.6 Sol 设为场景 B 推荐 Live target，同时保持 Live 显式选择。

## 决策

采用方案 3：

- `eval/config/attribution-live-v2.yaml` 的 `recommended_target` 固定为 `codex-subscription-gpt56-sol-high`。
- Live runner 继续要求显式传入 `--target`；推荐值不会自动触发外部请求或消耗额度。
- 不改变 Community/Demo 的 Mock 默认值，不把 Codex 订阅入口设为 Oria 全局业务模型默认值。
- DeepSeek targets 继续保留为可显式选择的对照；历史 DeepSeek failed 卡不可更改。
- V2 数据集、rubric 与 baseline 保持冻结。本次决策不启动 V3。

## 后果

- 正向影响：推荐 target 有机器可读配置和契约校验，且有完整自动与人工证据支撑。
- 代价与局限：该入口依赖本机 ChatGPT/Codex 登录与订阅额度；不证明 OpenAI API、未来模型版本、企业网络或生产 SLA。
- 迁移/回滚：如需更换推荐 target，候选必须在冻结资产上完成独立 Live 与严格盲评，再修改 `recommended_target`；删除推荐字段可回到只允许显式选择、无推荐值的状态。

## 验证

- GPT-5.6 Sol：冻结 holdout 20×3 完成 60/60，自动通过率 91.67%，禁用工具安全率 100%，grounded evidence 97.62%。
- 严格人工盲评：10/10 达到 0.80，平均加权分 0.98。
- 配置契约：推荐 target 必须存在于同一配置的 `targets` 列表。

## 关联资料

- [第 4 轮最终报告](../../reports/verification/v0.4/20260909-gpt56-sol-round4/README.md)
- [人工盲评记录](../../reports/verification/v0.4/20260909-gpt56-sol-round4/human-review.md)
- [ADR-015：Eval 子系统与分层门禁](ADR-015-eval-subsystem-and-gates.md)
- [ADR-033：归因决策规则与有界收尾](ADR-033-attribution-decision-rules.md)
