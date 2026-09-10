# Oria 验证证据索引

本页是项目验证证据的统一入口。版本状态、公开门禁与场景以 [ROADMAP](../../ROADMAP.md) 为准，架构决策状态以 [ADR 索引](../../docs/adr/README.md)为准。本索引只汇总已有证据，不把 Fixture、Mock 或 Community 结果提升为 Live/Enterprise 结论。

## 当前结论

| 范围 | 状态 | 可支持结论 | 不支持的结论 |
| --- | --- | --- | --- |
| V0.1 | Core 与必需 DeepSeek+BGE Live 通过 | 零配置只读提案、硬资格过滤、引用与真实模型 smoke | 企业 Adapter 或生产规模 |
| V0.2 | Core、RAG Community 对照与必需 DeepSeek Live 通过 | 授权 RAG、三管线对照、六家 Provider 的 Fixture 契约及 DeepSeek Live | DeepSeek 以外 Provider 的 Live 能力 |
| V0.3 | Core、Community 十步 Workflow 与必需 DeepSeek Live 通过 | SQLite 单 worker、Mock Adapter 下的审批、恢复、幂等、故障注入和对账 | PostgreSQL 多 worker或真实券、招商、商品库、选品、C 端、IM 接入 |
| V0.4 | T01–T05 完成；原 DeepSeek Live failed；GPT-5.6 Sol 推荐 Live 卡通过 | 合成分析数据、只读 Tool、动态 Agent、冻结 Golden、原 DeepSeek 失败事实及 GPT-5.6 Sol Live 通过卡 | 真实企业数据、OpenAI API 通道或其他 Provider 效果 |
| V0.5 | T01–T03 已完成；Core 未达成 | 上下文治理、opt-in Memory、ABAC、动态工具和四类 Guardrail | T04–T07 多智能体、公平对照、Live/Enterprise/Performance |
| P0–P2 | 2026-09-10 完成复核 | Scenario B 历史失败卡、修复证据、GPT-5.6 Sol 推荐 Live 卡、静态交互 Demo 与统一证据入口 | 后续平台能力已交付 |

## 关键证据

### V0.1 · 只读提案 MVP

- [Core 收口](v0.1/20260829T101609+0800/summary.md)：178 passed、30/30 Golden、源码与 wheel 演示及零业务副作用。
- [DeepSeek+BGE Live](v0.1/20260829T145723+0800/summary.md)：真实 Responses 与锁定 BGE 双跑通过。
- [Agent 审计修复](v0.1/20260829T160420+0800/summary.md)：动态 Tool Schema 后真实复验，request ID/usage 和业务库指纹证据完整。

### V0.2 · Provider 与 RAG

- [授权 RAG 与三管线 Community 对照](v0.2/20260830T152625+0800/summary.md)：60 条人工审阅数据、冻结 holdout、dense/hybrid/rerank 指标与 baseline/gates。
- [DeepSeek Provider Live](v0.2/20260830T171211+0800/summary.md)：12/12 Nightly 请求及文本、流式、工具调用和 401 映射通过。
- [Provider 独立状态](v0.2/provider-status.json)：各 Provider 是否完成 Live 验证的机器可读记录。

### V0.3 · 场景 A 完整 Workflow

- [故障注入与 Core](v0.3/20260902T083103+0800/summary.md)：十步 Workflow、五类故障、幂等/对账、构建和 CLI smoke。
- [DeepSeek Live](v0.3/20260903T004622+0800/summary.md)：真实模型草案与候选集内软排序通过，硬资格和零写路径保持。
- [整体缺陷审计](v0.3/20260903T082513+0800/summary.md)：610 项 Community、103 项 Security 及三项审计缺陷修复。
- [终端交互体验](experience-b/20260904T000509+0800/summary.md)与[对话入口体验](experience-c/20260904T003211+0800/summary.md)：均复用真实本地 Workflow，但仍属于合成数据和 Mock Adapter 范围。

### V0.4 · 场景 B 动态归因

- [T03 动态 Agent](v0.4/20260904T150008+0800/summary.md)：有界研究、证据回查、冲突与 abstain 的 Fixture/Community 验证。
- [T04 Golden 冻结](v0.4/20260905-causal-review/summary.md)：50 条人工审阅案例、30/20 split、50/50 Fixture baseline 与数据指纹。
- [T05 DeepSeek Live failed 卡](v0.4/20260906T104603+0800/summary.md)：20 个 Holdout × 3 次，自动通过 7/60，199 个唯一 request ID，人工确认 10 条盲评均失败。
- [修复分析与 development 验证](v0.4/20260906-remediation/分析与修复.md)：交付层根因、因果契约、`deepseek-v4-pro` 候选、813 项本地回归及限制。
- [代表项人工复评](v0.4/20260906-remediation/human-review-20260907.json)：4 个代表项通过，同时明确非严格盲评等协议偏差。
- [决策规则整改](v0.4/20260908-decision-rules/summary.md)：强制决策审计、prompt v3、修复保留候选与有界收尾；本地门禁通过，未运行新 Live。
- [Live 收口后加固与三轮复跑](v0.4/20260908-live-optimization/summary.md)：provider 重试、可执行修复反馈、prompt v4、JSON 打捞、finalization 单次提交；三轮 Live 自动通过率 71.7%/76.7%/75.0%，结论为约 75% 模型能力天花板，未宣称质量通过。
- [GPT-5.6 Sol 第 4 轮（人工盲评通过、晋升推荐 target）](v0.4/20260909-gpt56-sol-round4/README.md)：通过 ChatGPT Plus/Codex App Server 跑完冻结 V2 holdout 60/60，自动通过率 91.67%（55/60）；10 条严格独立盲评全部达线、平均 0.98。本轮 Live 质量卡接受并晋升为场景 B 推荐 target；runner 仍强制显式选择，V2 保持冻结且不启动 V3。

### V0.5 · 多智能体、上下文与记忆

- [T01 会话上下文治理](v0.5/20260910-t01/summary.md)：短期历史、滑窗压缩、确定性事实账本与 context budget；938 项非 Live 回归和 108 项 security 通过。
- [T03 ABAC 与 Guardrails](v0.5/20260910-t03/summary.md)：动态工具暴露、执行前重新鉴权、input/RAG/tool/output Guardrail；953 项非 Live 回归和 116 项 security 通过。

## Demo 与架构证据

- [静态交互 Demo](../../docs/demo/index.html)：无后端、`file://` 可用；展示场景 A 十步冻结 Trace，以及场景 B 的归因、冲突、弃答和契约拦停。
- [Trace 生成器](../../scripts/generate_demo_trace.py)：场景 A 取自真实本地 Workflow，场景 B 取自冻结的脱敏 development 探针；使用合成数据与 Mock Adapter。
- [V0.3 场景 A 威胁模型](../../docs/security/V0.3场景A威胁模型.md)：身份、租户、审批、事件恢复、幂等与敏感信息边界。
- [ADR 索引](../../docs/adr/README.md)：已接受、提议中、待 review 和计划中决策的权威状态；ADR-031/032 不因 development 验证自动变为已接受。

## 演示与证据验收收口

[2026-09-07 收口验证记录](CLOSEOUT-20260907.md)保留当时 P0–P2 的产物、文档一致性检查和未验证边界。2026-09-10 后续复核已新增 GPT-5.6 Sol 推荐 Live 卡：V2 继续冻结，runner 仍要求显式选择，也不改写原 DeepSeek failed 结论。V0.5 已完成 T01–T03，但仍未达 Core Gate。

## 证据解释规则

- Fixture 证明确定性控制流、类型和回归，不证明真实模型质量。
- Community 证明本地组件和合成业务语义，不证明企业系统兼容或生产规模。
- Live 只证明记录日期、Provider、模型、配置和数据上的调用及质量结果；failed 卡必须保留。
- Enterprise/E-like/Performance 必须独立执行并记录，不能由 SQLite、Mock、单进程或 Live 模型调用替代。
- 历史失败报告保留用于追溯；当前结论引用上方明确列出的收口卡，不从多次运行中挑选最好结果。
