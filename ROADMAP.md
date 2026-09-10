# Oria 执行计划

本文是 GitHub 可见的适度简略版执行计划：保留版本状态、任务依赖、主要产物和完成验证，省略逐字段规则、完整测试清单、详细真实验证场景和验证报告链接。

## 当前优先执行路线（`CODEX_TODO_ORIA_MAIN_PROJECT`）

当前先完成演示与证据验收，再继续平台扩展，执行顺序不可跳过：

1. **P0：Scenario B 收口**——**已收口（2026-09-10）**：V0.4-T05 原 DeepSeek 冻结 Live 卡以 failed 结论收口（不可更改）；GPT-5.6 Sol 随后在冻结 V2 holdout 上完成 60/60 并通过严格人工盲评，`FrankLee` 已确认将 `codex-subscription-gpt56-sol-high` 晋升为场景 B 推荐 Live target。Runner 仍强制显式选择，不改变 Mock 默认或自动发起 Live；V2 保持冻结，不启动 V3。
2. **P1：交互 Demo**——**已收口（2026-09-07）**：`docs/demo/` 静态页面（无后端、file:// 可直接打开）展示场景 A 十步冻结 Trace，以及场景 B 的归因、冲突、弃答与契约拦停案例；`oria attribution ask` 补充场景 B 单案例分步 CLI 演示，默认为开发集 Mock 回放，显式 `--llm-profile` 时才进入 Live 动态选路。数据由真实本地 Workflow/Graph、Mock Adapter 与合成数据产生，所有入口均声明 Mock/Live 边界。公开在线访问需在仓库设置启用 GitHub Pages（/docs 目录）。
3. **P2：证据索引与文档一致性**——**已复核（2026-09-10）**：统一验证证据索引已覆盖故障注入、RAG 对照、Scenario B Eval、Live 卡、Demo 与 ADR；当前文档已同步 V2 冻结状态、GPT-5.6 Sol 推荐结论、验证数字和能力边界。V0.5-T01–T06 现已完成。

演示和评测只使用贴近企业业务的版本化合成数据与 Mock Adapter，不接触企业内部敏感数据，也不把 Mock 结果表述为真实企业接入。真实业务 Adapter 作为有条件时的独立加分项，不阻塞本轮收口。

**门禁状态**：P0–P2 已完成，本轮演示与证据验收门禁关闭。V0.5-T01–T06 已完成并达到 Core Gate，V0.6 可按依赖实施；V0.5-T07 Live single/multi 对照仍是独立必需卡，完成前不声明多智能体质量提升。

## 版本状态总览

| 版本 | 定位 | 状态 | 一句话交付 |
| --- | --- | --- | --- |
| V0.1 | 场景 A 只读提案 MVP | T01–T10 已完成；Core 与必需 DeepSeek+BGE Live 卡通过 | 零配置 Demo 完成规则检索、硬资格商家预筛、LLM 软排序和带引用提案，且不产生业务写入。 |
| V0.2 | Provider 与 RAG 完整化 | T01–T06 已完成；Core、Nightly 与 DeepSeek 必需 Live 卡通过 | 统一六家 Provider 的 Fixture 契约，完成授权 RAG、三管线对照、冻结数据集和 DeepSeek Live 验证；其他 Provider 未 Live 验证。 |
| V0.3 | 场景 A 完整 Workflow | T01–T09 与 Core 已完成；DeepSeek Live 卡已通过 | 本地 SQLite、官方 AsyncSqliteSaver、Mock 企业 Adapter 和合成数据已跑通 10 步流程、双等待恢复、幂等与对账。 |
| V0.4 | 场景 B 动态归因 Agent | T01–T05 已完成；原 DeepSeek Live failed；GPT-5.6 Sol 推荐 Live 卡通过 | 50 条 Golden 已审阅并冻结 30/20 split；GPT-5.6 Sol 完成 20×3，自动通过 55/60，严格盲评 10/10 达线、平均 0.98。 |
| V0.5 | 多智能体、上下文与记忆 | T01–T06 与 Core 已完成；T07 Live 待执行 | 已交付上下文治理、opt-in Memory、ABAC/Guardrail、supervisor/Subagent、公平对照 harness 与 S2–S4 C/SEC 证据；不声明 Live 质量提升。 |

## V0.1：场景 A 只读提案 MVP

| ID | 依赖 | 任务与产物 | 完成验证 |
| --- | --- | --- | --- |
| V0.1-T01 | — | 建立 Python 3.11、uv 锁文件、src layout、CLI、pytest markers 与分层 CI 骨架。 | 工具版本、构建、CLI/import smoke、核心测试与非法 target 退出码可验证。 |
| V0.1-T02 | V0.1-T01 | 建立值类型、Protocol、Runtime/Context、主体模型、PolicyEngine、配置诊断和统一运行时装配骨架。 | 配置、权限、生命周期与并发 Context 隔离契约通过。 |
| V0.1-T03 | V0.1-T02 | 内置脱敏规则/商家资源，建立 platform/business migration、Merchant 领域模型和 EligibilityPolicy。 | wheel 资源、资格策略、空库升级与重复初始化通过。 |
| V0.1-T04 | V0.1-T02 | 实现 Mock/DeepSeek Provider、Fixture/BGE Embedder、流式、能力与结构化输出契约。 | Provider/Embedder 契约、严格输出校验和缺 Key 关闭路径通过。 |
| V0.1-T05 | V0.1-T03,V0.1-T04 | 实现 ObjectStore、文档 Catalog、Chroma 投影、Retriever、规则快照和逐字段引用。 | 摄入/检索/重建、六类规则、快照隔离与固定问题召回可验证。 |
| V0.1-T06 | V0.1-T03,V0.1-T05 | 实现两个只读 Tool、注册表、schema/allowlist 和硬规则商家过滤。 | 工具参数、资格结果、引用回查和敏感字段最小披露通过。 |
| V0.1-T07 | V0.1-T04,V0.1-T06 | 实现版本化 Prompt、提案 schema、官方 SQLite Saver、有界 StateGraph 与 30 条场景 A golden。 | Agent/Checkpoint 隔离、Graph 回归、候选子集、引用与零写工具门禁通过。 |
| V0.1-T08 | V0.1-T07 | 完成唯一 Runtime 装配、零配置 Demo、自动初始化、结构化输出和验证报告。 | 源码态与安装 wheel 均可离线重复运行，业务库保持零副作用。 |
| V0.1-T09 | V0.1-T08 | 完成 README、初版威胁模型、证据模板和 Core 报告。 | 文档命令可复现，证据完整且声明不过界。 |
| V0.1-T10 | V0.1-T09 | 执行真实 DeepSeek + 锁定 BGE 必需 Live 卡。 | Live 报告记录模型、revision、request ID 并通过。 |

## V0.2：Provider 与 RAG 完整化

| ID | 依赖 | 任务与产物 | 完成验证 |
| --- | --- | --- | --- |
| V0.2-T01 | V0.1-Core | 扩展四家 OpenAI-compatible profile、Anthropic 与 Mock 的统一能力、错误、流式和结构化输出契约。 | endpoint dialect 与输出策略矩阵的统一 Provider 契约通过。 |
| V0.2-T02 | V0.1-Core,V0.1-T02 | 增加 tenant/subject/read-policy/audit/outbox migration，将 PolicyEngine 扩展为默认拒绝的文档读取 ACL。 | migration、授权过滤和脱敏审计的契约/安全验证通过。 |
| V0.2-T03 | V0.2-T02,V0.1-T05 | 为知识目录增加 owner、ACL、classification 和版本策略，实现 AuthorizedRetriever 与索引重建。 | 更新删除传播、引用生命周期、跨租户与 ACL 隔离通过。 |
| V0.2-T04 | V0.2-T03 | 加入 BM25、dense fusion、reranker 和显式可选的检索管线。 | 三种管线遵守同一接口，失败时不静默降级。 |
| V0.2-T05 | V0.2-T03,V0.2-T04 | 建立 60 条人工审阅 RAG 数据、冻结 holdout、Eval harness、PR baseline/gates 与有预算的 Nightly。 | 数据污染检查、门禁路径和锁定 BGE 三管线 Community 对照完成。 |
| V0.2-T06 | V0.2-T01,V0.2-T05 | 执行 DeepSeek 必需 Live 卡，并为其他 Provider 分别维护状态。 | DeepSeek request/model/usage 证据通过；其他无 Key Provider 保持未 Live 验证。 |

## V0.3：场景 A 完整 Workflow

| ID | 依赖 | 任务与产物 | 完成验证 |
| --- | --- | --- | --- |
| V0.3-T01 | V0.2-Core,V0.1-T03 | 在既有 migration 上加入活动、券、报名、确认、选品、投放、通知等领域实体、状态机和 Repository。 | 领域不变量、tenant 复合约束、V0.1 升级与空库升级/回滚通过。 |
| V0.3-T02 | V0.2-T02,V0.3-T01 | 实现写 RBAC/职责分离、双审批、外部等待/inbox、受信事件绑定和动态业务确认链。 | 越权/自批拒绝，重复乱序事件不恢复，确认链与超时规则通过。 |
| V0.3-T03 | V0.3-T01,V0.3-T02 | 实现 execution ledger、规范化参数/计划哈希、receipt、domain/audit/outbox 与两库事务边界。 | 单库原子回滚、重复执行和对账通过；不伪造跨库事务。 |
| V0.3-T04 | V0.3-T03,V0.1-T07 | 实现活动草案、券物化、招商发布 Tool 与经审批的可恢复 LaunchPlan saga。 | 草案无外部副作用，审批篡改拒绝，部分成功进入补偿或对账。 |
| V0.3-T05 | V0.3-T03,V0.3-T04 | 实现商品快照与资格策略、三种报名模式、双来源汇聚、确认链、报名写入和券关联。 | 分页/规则版本、关窗、迟到事件、唯一键汇聚和无悬空关联通过。 |
| V0.3-T06 | V0.3-T03,V0.3-T05 | 实现异步选品、受信结果事件、C 端投放和商家通知的 Service/Tool/Mock Adapter。 | 仅合格入选商品可投放，结果变化使审批失效，unknown 与通知死信可收敛。 |
| V0.3-T07 | V0.3-T02,V0.3-T04,V0.3-T05,V0.3-T06 | 将完整 10 步预定流程接入原 Graph，加入双真实 interrupt、并行汇聚、外部事件等待和恢复 CLI。 | Graph/HITL/事件恢复、冲突 reducer、10 步 E2E-F 与 Mock 事件注入通过。 |
| V0.3-T08 | V0.3-T07 | 执行 Fixture/Community 故障注入、安全复核并形成 Core 证据。 | 五类故障、重复计数、状态机、最小权限、数据库与回执断言通过。 |
| V0.3-T09 | V0.3-T08,V0.2-T01 | 使用真实 DeepSeek 验证草案与候选集内软排序。 | **已完成**；`deepseek-v4-flash` Live 卡已于 2026-09-03 通过，LLM 不改变硬资格且没有直接写路径。 |

## V0.4：场景 B 动态归因 Agent

| ID | 依赖 | 任务与产物 | 完成验证 |
| --- | --- | --- | --- |
| V0.4-T01 | V0.3-Core | 构建固定 seed 的合成分析 schema/生成器，并将根因标签与生产查询库物理隔离。 | **已完成**；数据不变量、确定性生成和标签不可查询验证通过。 |
| V0.4-T02 | V0.4-T01,V0.2-T03 | 实现漏斗下钻、活动、大盘和历史经验等只读分析工具。 | **已完成**；SQLite 只读打开、固定参数化查询、可信 Context tenant、有界时间、证据 provenance 与授权 RAG 过滤已验证。 |
| V0.4-T03 | V0.4-T02,V0.1-T07 | 复用有界研究原语实现动态归因、evaluator-optimizer、引用、abstain 与预算终止。 | **已完成**；Prompt/Agent 契约、非固定调查路径、证据回查、冲突与 abstain 已通过 Fixture/Community 验证。 |
| V0.4-T04 | V0.4-T01,V0.4-T03 | 建立至少 50 条人工审阅 case、至少 20 条冻结 holdout、盲评 rubric 与 attribution eval CLI。 | **已完成**；50 条为 20 条可回答、24 条证据/权限不足和 6 条冲突证据，含 8 条受控变体的有限归因及 6 条真实会话历史案例；`FrankLee` 于 2026-09-05 审阅通过，30/20 split 已冻结，50/50 Fixture baseline 通过。 |
| V0.4-T05 | V0.4-T04 | 在冻结 holdout 上执行真实模型场景、重复采样、校准和 coverage-risk 报告。 | **已完成 / failed**；DeepSeek `deepseek-v4-flash` 已完成 60/60，自动通过 7/60，199 个 request ID 唯一，成本上界 1.12635028 美元；`FrankLee` 已确认 10 条盲评均失败，本轮质量卡已收口。 |

V0.4-T05 修复已收口（2026-09-07）：累计授权 4 美元；原批次 60 条 failed 卡与既有盲评结论不变。修复在候选配置 `deepseek-pro-structured`（deepseek-v4-pro）上完成，未晋升默认：交付层三根因（工具 `execution_id` 前缀诱导误抄、调查轮绕过最终化投影提交、根级校验修复反馈为空）已修复；development 复验 001 attributed 2/2、015 insufficient 2/2、020 conflicting 3/6（其余为因果契约 fail-closed，无错误答案流出）；本地 813 项通过；`FrankLee` 已复评 4 代表项全部通过并确认拦停正确（`human-review-20260907.json`，协议偏差已注明）。v4-pro 未核价，探针约 198 万 Token 不估算美元成本。详见 [修复记录](reports/verification/v0.4/20260906-remediation/分析与修复.md) 与 ADR-031/032；冻结 target 变更须严格盲评。

2026-09-08 决策规则整改：新增强制决策审计，将多候选冲突、证据缺口和输出一致性纳入程序校验；修复阶段保留候选与缺口，无进展进入有界收尾。冻结数据与历史报告不变，本轮未运行新 Live，不改变原 failed 卡或 V2 待人工盲评状态。见 [ADR-033](docs/adr/ADR-033-attribution-decision-rules.md) 与 [本轮本地证据](reports/verification/v0.4/20260908-decision-rules/summary.md)。

2026-09-09 Live 收口后加固与三轮复跑：在决策规则整改基础上做工程加固——可重试 provider 故障有界重试（不占用模型轮）、决策规则/schema 校验反馈翻译为可执行修复指令、新增 prompt v4（明确 support/refutation 索引不相交、保留全部 supported 候选、因果追问须覆盖 funnel+activity 证据）、structured 输出 JSON 打捞（去代码围栏/尾逗号/前后散文）、finalization 关闭并行工具调用以稳定保留提交、放宽 `evidence_indices` 为可选并补语义描述。本地 `make lint`/`make test` 878 项通过。随后用 `deepseek-pro-structured` 对冻结 holdout 复跑三轮 Live：自动通过率分别为 71.7%（43/60）、76.7%（46/60）、75.0%（45/60），`required_tool_coverage` 稳定 100%。结论：通过率卡在约 75% 的模型能力天花板——剩余失败集中在决策契约四元交叉对账（candidates/hypotheses/evidence.supports/outcome）、synthetic_tool 结构化输出偶发失败与幻觉工具名/伪造引用，均属 deepseek-v4-pro 能力上限而非工程缺陷；不把 75% 表述为质量通过或接近 100%，自动门禁仍为 `completed_pending_human_review`，须人工盲评。证据见 [本轮报告](reports/verification/v0.4/20260908-live-optimization/summary.md)。

2026-09-10 GPT-5.6 Sol 第 4 轮 Live 完成并通过人工盲评：新增 ChatGPT 订阅鉴权的 Codex App Server provider，固定 `gpt-5.6-sol` / `high` 并沿用冻结 V2 holdout 20×3；Runner 支持断点续跑和订阅额度触顶安全停批，分两阶段跑完全部 60/60 case-run。自动通过率 91.67%（55/60），outcome/abstain 96.67%、禁用工具安全 100%、grounded evidence 97.62%，三轮重复 85%/95%/95%；对比 deepseek-pro-structured 的 75% 提升约 16.7 个百分点。原 Live 运行时 `make lint` 与 886 项非 Live 测试通过。`FrankLee` 完成 10 条严格独立盲评，10/10 达线、平均 0.98，Live 质量卡接受。一次解盲后改分的样本已排除并以另一条未披露样本补足，原始评分与修订轨迹完整保留。`codex-subscription-gpt56-sol-high` 已晋升为场景 B 推荐 Live target，但 runner 仍强制显式选择；V2 保持冻结，不启动 V3。采用变更的 31 项定向测试、完整静态检查与 927 项非 Live 测试通过。原始运行 JSON 的 `completed_pending_human_review` 状态保持不可变；本结论不改变历史 DeepSeek failed 卡，也不等同于 API 或企业环境验证。见 [本轮报告](reports/verification/v0.4/20260909-gpt56-sol-round4/README.md)和 [ADR-034](docs/adr/ADR-034-gpt56-sol-recommended-live-target.md)。

## V0.5：多智能体、上下文与记忆

| ID | 依赖 | 任务与产物 | 完成验证 |
| --- | --- | --- | --- |
| V0.5-T01 | V0.4-Core | 实现短期历史、滑窗摘要、事实账本和统一 context budget。 | **已完成**；固定事实逐项保持、预算/溢出、Memory 契约与 agent 接入回归通过。 |
| V0.5-T02 | V0.2-T03,V0.5-T01 | 实现显式 opt-in Memory、tenant/subject 隔离、TTL、来源/置信/敏感级别及查看删除导出。 | **已完成**；生命周期、隔离和删除传播契约/安全验证。 |
| V0.5-T03 | V0.3-T02,V0.5-T02 | 完成 RBAC/ABAC、职责分离、动态工具暴露和输入/RAG/Tool/输出 Guardrail。 | **已完成**；默认拒绝、执行前重新鉴权、动态最小工具和投毒攻击安全验证通过。 |
| V0.5-T04 | V0.5-T01,V0.5-T03,V0.4-T03 | 建立 tool-based supervisor 与至少两个专职 Subagent，约束 handoff、allowlist 和循环上限。 | **已完成**；路由、权限不放大、失败回收的契约与 E2E-F 验证通过。 |
| V0.5-T05 | V0.5-T04,V0.4-T04 | **已完成**：建立 single/multi 等额预算、随机顺序、隐藏架构标签的公平对照 harness 与 `eval compare`。 | Fixture 仅作描述性证据；Live 价值判断留 T07。 |
| V0.5-T06 | V0.5-T02,V0.5-T03,V0.5-T04 | 执行 Community/Security 场景并更新威胁模型与 Memory 保留/删除说明。 | **已完成**；Core 报告覆盖删除、投毒、权限和跨会话生命周期。 |
| V0.5-T07 | V0.5-T05,V0.5-T06 | 执行 single/multi 必需 Live 对照，历史场景 B Live 仅作证据引用。 | Live 报告须如实记录通过、失败或阻塞及质量/成本/延迟/方差。 |

V0.5-T01 交付（已完成，Fixture/Community）：社区 runtime 默认挂载按 tenant + session 隔离的进程内短期 Memory，通过 UTF-8 确定性 Token 估算、system + 最近消息滑窗、规范 JSON 摘要与 checkpoint 事实账本治理单次模型输入。固定商家 ID/名称/金额/结论压缩前后逐项断言通过；完整非 Live 套件 938 passed、security 108 passed。本任务无真实网络、无 migration，`search()` 仍为 T02 stub；证据见 [V0.5-T01 验证卡](reports/verification/v0.5/20260910-t01/summary.md) 与 [ADR-035](docs/adr/ADR-035-context-budget-and-fact-ledger.md)。

V0.5-T03 交付（已完成，Fixture/Community/Security）：在原 RBAC/职责分离之后叠加 ABAC deny，动态工具暴露仅交付当前 PolicyDecision 允许的静态 allowlist 子集，执行前由 tool Guardrail 重新鉴权。input prompt/input RAG 只告警不参与授权，output safety 对 PII/凭证/确定性毒性模式脱敏。`make lint` 通过，完整非 Live/Enterprise/Performance 套件 953 passed，security 116 passed。本任务无 migration、无新依赖、无真实网络；证据见 [V0.5-T03 验证卡](reports/verification/v0.5/20260910-t03/summary.md) 与 [ADR-010](docs/adr/ADR-010-guardrails-and-hits.md)。

V0.5-T06 交付（已完成，Fixture/Community/Security）：S2–S4 跨会话 Memory、删除传播/脱敏审计、记忆投毒、三角色动态工具与越权审计断言通过；完整非 Live/Enterprise/Performance 套件 969 passed，security 117 passed。已新增 [V0.5 威胁模型](docs/security/V0.5威胁模型.md)、[Memory 保留/删除说明](docs/security/V0.5-Memory保留与删除.md) 和 [Core 验证卡](reports/verification/v0.5/20260910-t06/summary.md)。本证据不包含 Live/Enterprise/Performance；T07 Live 仍待执行。

## 验证分层说明

- **Fixture（F）**：使用 MockLLM、FixtureEmbedder、Mock Tool 和固定数据，证明控制流、类型契约、错误处理与确定性回归；不能证明真实模型或外部服务。
- **Community（C）**：使用本地 BGE、Chroma、SQLite、真实进程或网络回环，证明社区版本地链路、恢复和数据一致性；不能证明企业规模或企业系统兼容性。
- **Live（L）**：调用明确记录的真实公开模型 API，只证明该日期、模型和配置下的调用与质量结果；不能外推到其他模型或未来版本。
- **Enterprise（E-like/E）**：E-like 使用本地 PostgreSQL、Milvus、Redis、OTel 等企业栈组件，E 使用真实企业环境与 Adapter；两者均按目标独立验证，不能互相或由 Mock 替代。

当前已验证到：V0.1/V0.2/V0.3 Core 与各自必需 DeepSeek Live 卡均已通过；V0.3 Community 使用 SQLite、官方 AsyncSqliteSaver、Mock 企业 Adapter 和合成数据完成验证。V0.4 T01–T05、合成数据、标签隔离、五个只读归因 Tool、动态 Agent、人工审阅 Golden 与冻结 Fixture baseline 已完成；原 DeepSeek Live 卡 failed，GPT-5.6 Sol 推荐 Live 卡已通过严格人工盲评。V0.5-T01–T06 的上下文、opt-in Memory、ABAC/Guardrail、supervisor/Subagent、对照 harness 与 S2–S4 Fixture/Community/Security 回归已通过，V0.5 Core Gate 已达成。真实企业 Adapter、OpenAI API 通道、Codex 与 DeepSeek 以外 Provider、E-like 多 worker 和 V0.5-T07 单/多 Agent Live 对照仍未验证。
