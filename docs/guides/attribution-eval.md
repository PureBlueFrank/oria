# 场景 B 动态归因评测

> 本页是冻结数据集评测入口。面向人的单案例分步演示请使用 [`oria attribution ask`](attribution-demo.md)；后者默认只允许 development，不读取 holdout，也不替代 baseline/gate。

场景 B 的冻结评测入口是：

```bash
uv run oria eval run --suite attribution \
  --split all \
  --report .artifacts/eval/attribution_v1.json
```

该命令使用固定合成分析数据、MockLLM 回放和真实的有界归因 Graph，不访问网络。报告包含逐例结果、结构化指标和去除 split、根因标签、Golden rationale、Provider 与架构标签的盲评包。

## 当前门禁

数据集未完成人工审阅时，命令以退出码 2 和 `eval_blocked` 结束，不运行任何案例。只有 `eval/datasets/scenario_b/manifest.json`、全部案例 review、Holdout 冻结状态和 rubric 哈希一致时才会开始评测。

Fixture 评测只证明评测机制、工具边界、证据引用与弃答结构可重复执行，不证明真实模型归因质量。真实 DeepSeek 的 S1–S3、重复采样、人工校准和 coverage-risk 报告由 V0.4-T05 单独执行。

## Split 规则

- `development`：30 条，可用于开发评测机制；不得用来声称冻结泛化质量。
- `holdout`：20 条，人工批准后冻结；使用后不能原地修改答案，变更必须新增数据集版本。
- `all`：仅用于确定性 Fixture baseline 和完整回归。

盲评标准见 `eval/config/attribution-rubric-v1.yaml`，人工审阅步骤见 `eval/datasets/scenario_b/REVIEW.md`。

## 2026-09-05 证据复核后的限制

50 条修订全文见 `eval/datasets/scenario_b/CASES.md`。固定 seed、案例 ID 和 30/20 分组保持不变；引用数值由实际只读查询重新计算，并按案例逐一对照。除标准数据外，评测按案例生成活动退出、大盘同向、多事件重叠、上下游并行异常和缺失活动记录等受控变体。

当前分布为 20 条 `attributed`、24 条 `insufficient` 和 6 条 `conflicting`。8 条非空 `root_cause_code` 仅用于受控合成变体：活动退出前后反转、稳定区域/大盘对照及相邻漏斗环节共同限定结论；这不是对真实业务机制的外推。6 条冲突案例均含两组以上实际可观测信号，039—044 均向 Graph 传入完整 user/assistant 历史消息对。

Fixture 回放仍按参考答案输出，其通过率不构成真实模型因果质量的证据。当前事实检查是独立的 SQLite 查询回归，评测查询范围已根据案例的区域、品类、商家和对照需求动态绑定。Golden v1 已由 `FrankLee` 审阅，Holdout 和 Fixture baseline 已冻结；任何内容变更必须创建新数据集版本。

## T05 真实模型评测

真实模型入口固定使用 `eval/config/attribution-live-v1.yaml`，只接受显式的已知 target：

```bash
uv run python scripts/run_attribution_live.py \
  --target deepseek \
  --preflight-only
```

预检在创建 Provider 前核对数据集、rubric、Fixture baseline、定价快照、20 条冻结 Holdout、三类结果覆盖、3 次重复和全部硬预算。缺少 `DEEPSEEK_API_KEY` 时输出 `request_count: 0` 的 blocked 卡，不发生外部请求。

移除 `--preflight-only` 后才会执行完整的 `20 × 3 = 60` 个 case run。运行顺序按三种预期结果分层，并对每次重复使用固定 seed 打乱；每例最多 4 次模型调用、10 次只读工具调用、96000 input token、24000 output token、0.075 美元和 240 秒，总体最多 240 次模型调用、576 万 input token、144 万 output token、1.35 美元和 7200 秒。单例 Token 预留覆盖多轮工具结果，4 轮模型硬限负责抑制无结论循环；报告保存逐例模型结论、ToolResult、事件、request ID、Token、成本口径和延迟，并汇总任务成功、abstain、工具/证据正确率、三次方差、固定阈值 coverage-risk 与置信度分桶。

每次执行都在 `--data-dir/runs/<timestamp>` 下生成独立的 Runtime 和合成 SQLite 夹具，因此重跑不会覆盖或复用前一轮数据。

模型置信度对正确弃答转换为决策置信度后才参与描述性统计。Brier/ECE 因重复样本相关且语义正确性尚未人工校准，只作为描述，不进入质量门禁。运行完成还会生成至少 10 条不含 Golden 标签、Provider 或架构身份的盲评包；必须由人工独立评分后，Live 卡才可验收。

2026-09-06 已完成 DeepSeek `deepseek-v4-flash` 的 60/60 冻结 Holdout 运行，自动通过 7/60，成本上界 1.12635028 美元。运行完整但自动质量失败，`FrankLee` 已确认 10 条盲评均失败，本轮以 failed 卡收口；证据见 `reports/verification/v0.4/20260906T104603+0800/summary.md`。该结果不得表述为场景 B 真实模型质量通过。

修复进展：研究图已预留提交和一次结构修复轮次，原生结构化输出最终阶段显式关闭工具，并为已有证据后的可恢复参数错误/工具预算拒绝提供最终提交机会。开发集真实探针仍存在结构化输出失败，Live 卡继续为 failed；分析、请求标识及成本记录见 `reports/verification/v0.4/20260906-remediation/分析与修复.md`。

候选配置 `deepseek-structured` 已显式加入，使用同一 Responses 模型和专用结构化提交函数；最终提交包含由原工具结果生成的数组位置索引。单条开发案例已完成 attributed 输出并通过 22 条精确证据校验，仍有因果措辞及三类案例复验待办；不替换默认 `deepseek` 或冻结 Live target。候选决策与采用条件见 `docs/adr/ADR-031-deepseek-structured-candidate.md`。

2026-09-06 增补：新增 `deepseek-pro-structured` 候选（deepseek-v4-pro），交付层三个根因已修复——工具 `execution_id` 的 `tool_` 前缀诱导误抄（改 `exec_` 并在投影中剥离）、调查轮可主动提交绕过最终化投影（改为仅最终阶段暴露提交函数）、根级校验修复反馈为空（改为携带规则文案）。v4-pro 开发复验：001 attributed 2/2、015 insufficient 2/2、020 conflicting 3/6（其余为契约 fail-closed，无错误答案流出）。候选未晋升默认配置，冻结 Live 卡继续为 failed，待新独立盲评。开发探针入口 `scripts/run_attribution_dev_probe.py`（仅接受 development 案例）。
