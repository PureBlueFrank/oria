# Scenario A v1 人工审阅清单

`v1.jsonl` 是 V0.1-T07 的 30 条 Golden 候选，当前全部标记为 `pending_human_review`，尚未冻结，不得用于创建 baseline 或宣称 Golden 门禁通过。

人工审阅者需逐条确认：

1. 输入表达的业务意图与 `expected_outcome` 一致。
2. 正常场景的 10 个 ID 是当前 Fixture 硬资格集；`demo-m003` 和 `demo-m004` 不得进入提案。
3. 六类规则缺失/冲突、权限拒绝、伪造引用、写工具注入和无进展的期望结果符合架构规范。
4. 不包含真实商家、客户数据、凭证或内部敏感字段。
5. 审阅通过后，将每条 `review.status` 改为 `approved`，填写真实 `reviewed_by` 和带时区的 `reviewed_at`，再生成新 manifest 和首个 baseline。

自动生成或 AI 检查不构成“实际人工审阅”。
