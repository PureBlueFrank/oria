# ADR-033：归因决策规则与有界收尾

- 状态：已实现，本地验证；未进行新 Live 验收
- 日期：2026-09-08
- 关联：V0.4-T03/T05、ADR-032

## 问题与范围

仅检查 outcome 的字段形状和引用原值，不能阻止模型删掉冲突候选、把工具限制说明标为成功、用描述性事实替代因果回答。原 no-progress 在已有可用证据时也直接终止。

本轮仅调整生产决策契约、prompt、validator、repair 和收尾逻辑。所有数据集、V2 冻结 holdout、rubric、baseline、target 及历史 reports 保持不变。不写入 case ID、地区、日期或预期答案规则，不读取评测标签。

## 决策

新生成结果使用 AttributionSubmission，强制包含 decision_assessment 和 causal_assessment；历史 AttributionConclusion 仍允许缺省审计字段。运行时 finalizer 强制新契约，不能靠 Provider 未执行 JSON Schema 验证绕过。

审计记录原任务类型、请求范围可用性、必需数据缺口、候选解释清单及其支持/反证引用索引。规则依次为：

1. 请求范围不可用：insufficient。
2. 范围内两个以上仍有证据支持的候选：conflicting；缺少区分依据不能改成 insufficient。
3. 其余情况下缺少必需证据或没有支持候选：insufficient。
4. 恰好一个支持候选且无必需缺口：允许 attributed。

沿用 outcome 与 conclusion/hypotheses/abstained/requested_data 的一致性校验。所有支持候选必须保留；排除候选须有独立反证索引。索引不得越界或指向 null；支持关系必须与 evidence.supports 一致；引用仍逐项回查成功 ToolResult 的原始值。

修复草稿保存在有界 checkpoint 状态中，不写入事件日志。进入仅提交阶段后，禁止变更任务类型、恢复不可用范围、删除已声明的必需缺口；此前有真实证据的候选及支持观察不得消失，可以在保留支持观察的同时补充已有反证将候选排除。结构问题提供字段路径和规则消息，有限重试耗尽后失败；伪造引用继续立即失败。不在程序中把无效答案自动改成成功弃答。

Prompt 使用新 v3，保留 v1/v2。明确“为何”不能偷换成“是否”、同一环节也可能多因、大盘其他细分稳定不能排除当前细分变化、null 比较不等于稳定。Live 评测指纹增加 prompt 版本、响应 Schema 和决策契约版本，旧卡不代表新契约验证结果。

Scenario B 的 no-progress 转入一次仅提交阶段，关闭调查工具、复用现有证据，允许合法事实报告、冲突或弃答；不增加模型/Token/工具/成本/时间上限。提交不合法时走现有有界 repair；继续请求业务工具则立即失败。Scenario A 保留原 no-progress 终止方式。

Fixture replay 仅补齐新提交字段以验证装配，不改变冻结题目或预期答案。其决策输入属于确定性回放，不是模型能力证据。

## 限制与验收

规则确定性约束已声明的决策事实及其引用，不能从任意自然语言自动证明因果或保证第一次候选盘点完整。模型仍可能漏报候选、误判原问题类型，或把语义无关但真实的值充作支持/反证。不得把结构门禁通过声明成 Live 质量通过；后续需在不改冻结资产的前提下另行显式运行真实验证并独立盲评。

本轮证据见 [决策规则验证](../../reports/verification/v0.4/20260908-decision-rules/summary.md)。新 Live 未运行；不修改历史 failed 卡，不启动 V0.5。
