# 场景 B 动态归因评测

> 本页是冻结数据集评测入口。面向人的单案例分步演示请使用 [`oria attribution ask`](attribution-demo.md)；后者默认只允许 development，不读取 holdout，也不替代 baseline/gate。

场景 B 的 Fixture 评测入口是：

```bash
uv run oria eval run --suite attribution \
  --split all \
  --report .artifacts/eval/attribution_v1.json
```

该命令使用固定合成分析数据、MockLLM 回放和真实的有界归因 Graph，不访问网络。报告包含逐例结果、结构化指标和去除 split、根因标签、Golden rationale、Provider 与架构标签的盲评包。

无额外参数时该 CLI 保留 V1 默认值以兼容历史基线。当前冻结 V2 需显式绑定 manifest 与 rubric：

```bash
uv run oria eval run --suite attribution \
  --manifest eval/datasets/scenario_b/v2.manifest.json \
  --rubric eval/config/attribution-rubric-v2.yaml \
  --split all \
  --report .artifacts/eval/attribution_v2.json
```

## 当前门禁

数据集未完成人工审阅时，命令以退出码 2 和 `eval_blocked` 结束，不运行任何案例。只有 `eval/datasets/scenario_b/manifest.json`、全部案例 review、Holdout 冻结状态和 rubric 哈希一致时才会开始评测。

Fixture 评测只证明评测机制、工具边界、证据引用与弃答结构可重复执行，不证明真实模型归因质量。真实模型的重复采样、人工校准和 coverage-risk 报告由独立 Live runner 执行。

## Split 规则

- `development`：30 条，可用于开发评测机制；不得用来声称冻结泛化质量。
- `holdout`：20 条，人工批准后冻结；使用后不能原地修改答案，变更必须新增数据集版本。
- `all`：仅用于确定性 Fixture baseline 和完整回归。

V1/V2 盲评标准分别见 `eval/config/attribution-rubric-v1.yaml` 与 `eval/config/attribution-rubric-v2.yaml`，人工审阅步骤见 `eval/datasets/scenario_b/REVIEW.md`。

## 2026-09-05 证据复核后的限制

50 条修订全文见 `eval/datasets/scenario_b/CASES.md`。固定 seed、案例 ID 和 30/20 分组保持不变；引用数值由实际只读查询重新计算，并按案例逐一对照。除标准数据外，评测按案例生成活动退出、大盘同向、多事件重叠、上下游并行异常和缺失活动记录等受控变体。

当前分布为 20 条 `attributed`、24 条 `insufficient` 和 6 条 `conflicting`。8 条非空 `root_cause_code` 仅用于受控合成变体：活动退出前后反转、稳定区域/大盘对照及相邻漏斗环节共同限定结论；这不是对真实业务机制的外推。6 条冲突案例均含两组以上实际可观测信号，039—044 均向 Graph 传入完整 user/assistant 历史消息对。

Fixture 回放仍按参考答案输出，其通过率不构成真实模型因果质量的证据。当前事实检查是独立的 SQLite 查询回归，评测查询范围已根据案例的区域、品类、商家和对照需求动态绑定。Golden V1 与 V2 均已由 `FrankLee` 审阅，Holdout 和 Fixture baseline 已分别冻结；任何内容变更必须创建新数据集版本。

## T05 真实模型评测

Live runner 只接受显式的已知 target。当前推荐入口固定使用冻结 V2 配置：

```bash
uv run python scripts/run_attribution_live.py \
  --config eval/config/attribution-live-v2.yaml \
  --target codex-subscription-gpt56-sol-high \
  --preflight-only
```

预检在创建 Provider 前核对数据集、rubric、Fixture baseline、定价快照、20 条冻结 Holdout、三类结果覆盖、3 次重复和全部硬预算。推荐 target 通过本机 Codex/ChatGPT 登录态检查可用性；DeepSeek target 则要求 `DEEPSEEK_API_KEY`。条件缺失时输出 `request_count: 0` 的 blocked 卡，不发生外部请求。

移除 `--preflight-only` 后才会执行完整的 `20 × 3 = 60` 个 case run。预算、模型轮数、工具次数、Token、成本和超时全部来自选定配置，不以文档中的历史 V1 数值替代。运行顺序按预期结果分层，并对每次重复使用固定 seed 打乱；报告保存逐例模型结论、ToolResult、事件、request ID、Token、成本口径和延迟，并汇总任务成功、abstain、工具/证据正确率、三次方差、固定阈值 coverage-risk 与置信度分桶。

Runner 支持 `--max-new-case-runs` 安全分批，并以 `--resume` 从已有报告恢复；恢复前重新核对 target、模型、数据集、rubric、baseline 与定价身份。已有输出路径默认拒绝覆盖。即使配置声明 `recommended_target`，runner 仍强制传入 `--target`，不会自动发起外部调用或消耗订阅额度。

每次执行都在 `--data-dir/runs/<timestamp>` 下生成独立的 Runtime 和合成 SQLite 夹具，因此重跑不会覆盖或复用前一轮数据。

模型置信度对正确弃答转换为决策置信度后才参与描述性统计。Brier/ECE 因重复样本相关且语义正确性尚未人工校准，只作为描述，不进入质量门禁。运行完成还会生成至少 10 条不含 Golden 标签、Provider 或架构身份的盲评包；必须由人工独立评分后，Live 卡才可验收。

### 历史 DeepSeek 与当前推荐结论

2026-09-06 已完成 DeepSeek `deepseek-v4-flash` 的 V1 60/60 冻结 Holdout 运行，自动通过 7/60，成本上界 1.12635028 美元。运行完整但自动质量失败，`FrankLee` 已确认 10 条盲评均失败，本轮以 failed 卡收口；证据见 `../../reports/verification/v0.4/20260906T104603+0800/summary.md`。该历史结果不得表述为场景 B 真实模型质量通过。

后续研究图增加提交修复、显式因果/决策审计和有界收尾；`deepseek-pro-structured` 在冻结 V2 上三轮自动通过率为 71.7%/76.7%/75.0%，未通过严格人工验收，也未晋升推荐 target。历史分析见 [修复记录](../../reports/verification/v0.4/20260906-remediation/分析与修复.md)与[三轮复跑报告](../../reports/verification/v0.4/20260908-live-optimization/summary.md)。

2026-09-10，`codex-subscription-gpt56-sol-high` 在冻结 V2 holdout 上完成 60/60，自动通过率 91.67%，并以 10/10 达线、平均 0.98 通过严格人工盲评，现为场景 B 推荐 Live target。V2 数据集、rubric 与 baseline 保持冻结，不启动 V3。完整证据见[第 4 轮最终报告](../../reports/verification/v0.4/20260909-gpt56-sol-round4/README.md)，采用边界见 [ADR-034](../adr/ADR-034-gpt56-sol-recommended-live-target.md)。
