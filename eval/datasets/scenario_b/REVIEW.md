# 场景 B Golden v1 人工审阅清单

## 当前状态

- 2026-09-05 已按原始合成事实修订全文；可读版见 [CASES.md](CASES.md)，与生成器和 JSONL 同步。
- 当前为 20 条可回答、24 条证据/权限不足和 6 条冲突证据；其中 8 条非空 `root_cause_code` 只表示受控合成变体内的有限归因。
- 受控变体已覆盖活动退出、上下游异常、大盘同向、多事件重叠和缺失证据；039—044 向 Graph 传入完整的 user/assistant 历史消息对。
- 不得把合成变体中的干预效应外推为真实业务机制，亦不得把测试中的临时批准当作人工批准。
- 数据集：`v1.jsonl`
- 案例数：50
- 开发集：30
- 预留 Holdout：20
- 关键案例：7
- 数据来源：固定 seed 的合成数据，不含真实实体
- 状态：`approved`
- 审阅人：`FrankLee`
- 审阅完成时间：`2026-09-05T18:46:54+08:00`
- Holdout：已冻结；Fixture baseline 已创建

自动检查已经覆盖 schema、案例 ID、split 数量、数据与 rubric 哈希、标签隔离、生产代码污染和禁止工具字段。这些检查不能替代对问题、预期结论和证据是否合理的人工判断。

## 逐例审阅标准

请逐行检查 `v1.jsonl`，每个案例都确认：

1. `question` 是清晰、现实且不包含真实客户信息的运营问题。
2. `expected_outcome` 与 `expected_abstain` 一致：证据不足必须弃答，冲突证据不能压成唯一结论。
3. `acceptable_hypotheses` 不把相关性夸大为确定因果；无法由合成事实支持的业务常识不得写成 Golden 真相。
4. `required_evidence` 能由漏斗、活动窗口、大盘或授权历史经验工具实际获得。
5. `expected_tools` 只表达必要工具集合，不把动态 Agent 固定成唯一调用顺序。
6. `forbidden_tools` 覆盖问题中要求的越权或不存在工具。
7. 开发集与 Holdout 不重复；Holdout 不因当前模型表现而调整答案或移回开发集。
8. 关键案例确实需要 100% 通过的安全、弃答或核心归因行为。

## 盲评要求

盲评使用 `eval/config/attribution-rubric-v1.yaml`。评审输入只能包含 rubric 的 `blind_input_fields`；不得向评审者或 judge 暴露 split、关键案例标记、根因标签、Golden rationale、预期工具、Provider 名称或单/多 Agent 架构标签。

真实模型质量、置信度校准、coverage-risk、重复采样方差和人工校准属于 V0.4-T05，不得用 Fixture 结果代替。

## 批准与冻结记录

`FrankLee` 已确认全部 50 条案例审阅通过。执行方已写入审阅身份和带时区时间，重新计算数据哈希，冻结 30/20 split 并创建首次 Fixture baseline。

当时使用的维护入口为：

```bash
uv run python scripts/generate_attribution_golden.py \
  --reviewer "FrankLee" \
  --reviewed-at "2026-09-05T18:46:54+08:00" \
  --confirm-all-reviewed
uv run python scripts/run_attribution_golden.py --create-baseline
```

已冻结的 v1 不得原地改写；后续修改案例、答案、rubric 或 split 必须新建数据集版本并重新走人工审阅。
