# ADR-031：DeepSeek 专用结构化提交候选配置

- 状态：提议中（显式开发验证配置，未替代默认配置）
- 日期：2026-09-06
- 关联任务：V0.4-T05 修复

## 背景

DeepSeek 原生 JSON Schema 在简单请求成功，但含工具历史的受控请求出现解释文字和代码块。完整业务案例反复无法结构化提交。专用提交函数的最小 Live 成功，完整业务验证进一步暴露领域约束及精确引用问题。不能以接口成功推定归因质量通过。

## 候选方案

1. 继续默认原生 JSON Schema，保持 ADR-001 的原配置语义。
2. 显式增加 deepseek-structured 候选配置，复用已实现的 synthetic_tool 规范形和保留函数。
3. 宽松提取任意正文中的 JSON。该方案不能处理 DSML 调查文本，也容易掩盖输出契约失败，不采用。

## 开发验证范围

实施方案 2 的独立候选，不改变默认 deepseek 配置和冻结 Live target。候选仍使用同一模型、密钥、Responses dialect 和研究图，不增加第二套 Agent 循环。

最终阶段仅暴露 `__oria_submit_response__`，指定该函数而非允许任意历史工具；adapter 将完整已完成的非系统消息记录封装为 JSON 数据，将系统约束按原顺序合并。成功结果中 data 的顶层数组逐项呈现为 `{data_path, value}`，路径指向原始数据，value 保持原值与类型；该索引仅为呈现元数据，不是新业务证据。不做摘要或自动修正模型引用。工具记录始终标记为不受信数据，不提升为系统指令。checkpoint 中的原会话不变。

2026-09-06 增补（同候选范围）：① deepseek synthetic_tool 候选仅在 `tool_choice="required"` 的最终阶段暴露提交函数——此前调查轮也暴露，模型在原生历史中主动提交、绕过投影并持续引用失败；② 投影信封剥离 `execution_id`/`idempotency_key` 内部审计字段，且工具侧 `execution_id` 前缀改为 `exec_`——原 `tool_` 前缀被模型 100% 误抄为 `tool_call_id`；③ 提交指令附本轮有效 `(tool_call_id, tool_name)` 白名单；④ 证据字段描述明令禁止引用 `execution_id`。上述均为模型可见数据的呈现修正，校验规则、Golden 与冻结 target 不变。

保留函数由 Provider 解析为 structured_output，不进入业务 ToolExecutor。JSON Schema、领域规则及证据逐值校验继续执行；不自动把失败归因改成通过的弃答。

## 验证与正式采用条件

- 原配置继续解析为 native_json_schema；候选配置必须显式选择，配置指纹不同。
- 验证最终请求强制保留函数、完整记录不丢失、工具数据不提权、输入 Message 不被修改。
- 原有 Provider 的 native/synthetic/混合调用拒绝与场景 A 回归继续通过。
- 开发集分别验证 attributed、conflicting、insufficient，包括语义正确性；之后建立新的独立盲评。
- 只有验证通过并完成评审后，才可调整冻结 Live target；本记录不宣称候选 Live 质量已通过，也不替代 ADR-001。

## 证据

见 `reports/verification/v0.4/20260906-remediation/分析与修复.md`。最小保留函数 Live request ID：db5ed771-eb90-4c89-a233-dccd63531ba1。开发复验已有完整结构与精确证据通过样本，但仍存在引用错误和冲突案例错误单因归因。增加调查轮数及提示词自查未可靠解决，候选不得晋升默认配置。

2026-09-06：新增 `deepseek-pro-structured` 候选（deepseek-v4-pro）。交付层根因修复后，development 三关注案例复验：001 attributed 2/2、015 insufficient 2/2（均通过逐值证据校验），020 conflicting 3/6 干净通过、3 次 fail-closed（2 次因果契约拦截合并单因、1 次非法参数硬失败）。三类（attributed/conflicting/insufficient）语义验证已完成；候选仍未晋升默认配置，待新独立盲评。

2026-09-07：`FrankLee` 复评 4 代表项（三类通过各一 + 拦停样本）全部通过并确认拦停正确，记录见 `reports/verification/v0.4/20260906-remediation/human-review-20260907.json`（协议偏差已注明：非严格盲评、逐项整体评分、抽样 3/7）。本评审完成开发验证的语义评审环节；候选晋升默认或冻结 target 变更仍需以严格盲评（≥10 条、隐藏标签）为前提另行决策。
