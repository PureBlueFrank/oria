# RAG v1 人工审阅清单

## 数据集范围

- 共 60 条全合成查询，42 条 development、18 条 holdout。
- 六类招商规则各 10 条：基础信息、招商范围、报名规则、优惠档位、确认规则、商家端素材。
- 每条只标注应命中的规则类别，不包含期望排序或答案措辞；评测标签不会进入检索索引。
- 数据源固定为 `demo-campaign-rules@1.0.0`，不含真实商家、客户或个人信息。

## 审阅要求

逐条确认以下事项：

1. 查询语义与 `expected_rule_category` 一致，且不是靠类别内部名泄露答案。
2. 查询自然、无歧义；如果可能同时命中多类，应修改或标注为不通过。
3. 查询中的每个概念都必须在固定源文档中明确存在；不得使用只存在于模型 schema、其他模块或推断中的规则。
4. development 与 holdout 均覆盖六类规则，且每个 split 必须有六条关键用例；不得根据 holdout 结果反向修改检索实现。
5. 不得靠直接复述六类 chunk 标题构造大量低难度样本；应优先使用字段级、事实级用户问法。
6. 不含真实实体、凭证、客户数据、隐藏提示或越权指令。
7. `critical=true` 的 12 条覆盖六类规则的主要用户问法，并按 development 6 条、holdout 6 条分布。

审阅完成前，manifest 保持 `pending_human_review`，不得创建 baseline、启用 PR gate 或执行冻结 holdout 的正式 BGE 对照。

## 审阅记录

- 状态：`approved`
- 审阅人：`FrankLee`
- 审阅时间：`2026-08-30T12:02:57+08:00`
- 批准范围：修订后的 60 条 RAG v1 case、development/holdout 分配和 12 条 critical 标记。
- 后续约束：holdout 自此冻结；任何 case、split、critical 或预期标签变更都必须升级 dataset version 并重新人工审阅。
