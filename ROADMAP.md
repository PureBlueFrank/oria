# Oria 执行计划

本文是 GitHub 可见的适度简略版执行计划：保留版本状态、任务依赖、主要产物和完成验证，省略逐字段规则、完整测试清单、详细真实验证场景和验证报告链接。

## 版本状态总览

| 版本 | 定位 | 状态 | 一句话交付 |
| --- | --- | --- | --- |
| V0.1 | 场景 A 只读提案 MVP | T01–T10 已完成；Core 与必需 DeepSeek+BGE Live 卡通过 | 零配置 Demo 完成规则检索、硬资格商家预筛、LLM 软排序和带引用提案，且不产生业务写入。 |
| V0.2 | Provider 与 RAG 完整化 | T01–T06 已完成；Core、Nightly 与 DeepSeek 必需 Live 卡通过 | 统一六家 Provider 的 Fixture 契约，完成授权 RAG、三管线对照、冻结数据集和 DeepSeek Live 验证；其他 Provider 未 Live 验证。 |
| V0.3 | 场景 A 完整 Workflow | T01–T08 与 Core 已完成；T09 DeepSeek 草案/软排序 Live 卡未执行 | 本地 SQLite、官方 AsyncSqliteSaver、Mock 企业 Adapter 和合成数据已跑通 10 步流程、双等待恢复、幂等与对账。 |
| V0.4 | 场景 B 动态归因 Agent | T01 已启动且首批产物已提交；T02–T05 未开始 | 已建立可复现合成分析数据并将根因标签与查询库隔离；查询 Tool、Agent、冻结集和 Live 评测尚未交付。 |
| V0.5 | 多智能体、上下文与记忆 | 未开始 | 计划交付上下文压缩、可治理 Memory、完整权限与 Guardrails、多智能体编排及公平对照实验。 |

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
| V0.3-T09 | V0.3-T08,V0.2-T01 | 使用真实 DeepSeek 验证草案与候选集内软排序。 | **未执行**；完成后须证明 LLM 不改变硬资格且没有直接写路径。 |

## V0.4：场景 B 动态归因 Agent

| ID | 依赖 | 任务与产物 | 完成验证 |
| --- | --- | --- | --- |
| V0.4-T01 | V0.3-Core | 构建固定 seed 的合成分析 schema/生成器，并将根因标签与生产查询库物理隔离。 | **已完成**；数据不变量、确定性生成和标签不可查询验证通过。 |
| V0.4-T02 | V0.4-T01,V0.2-T03 | 实现漏斗下钻、活动、大盘和历史经验等只读分析工具。 | 待完成；需验证 SQL 只读、tenant/时间范围与证据 provenance。 |
| V0.4-T03 | V0.4-T02,V0.1-T07 | 复用有界研究原语实现动态归因、evaluator-optimizer、引用、abstain 与预算终止。 | 待完成；需验证 Prompt/Agent 契约和非固定调查路径。 |
| V0.4-T04 | V0.4-T01,V0.4-T03 | 建立至少 50 条人工审阅 case、至少 20 条冻结 holdout、盲评 rubric 与 attribution eval CLI。 | 待完成；需验证数据 schema、污染隔离和 golden 冻结。 |
| V0.4-T05 | V0.4-T04 | 在冻结 holdout 上执行真实模型场景、重复采样、校准和 coverage-risk 报告。 | 待完成；Live 卡须保存逐例结果、方差与人工校准证据。 |

## V0.5：多智能体、上下文与记忆

| ID | 依赖 | 任务与产物 | 完成验证 |
| --- | --- | --- | --- |
| V0.5-T01 | V0.4-Core | 实现短期历史、滑窗摘要、事实账本和统一 context budget。 | 压缩前后事实保持、预算与溢出单元验证。 |
| V0.5-T02 | V0.2-T03,V0.5-T01 | 实现显式 opt-in Memory、tenant/subject 隔离、TTL、来源/置信/敏感级别及查看删除导出。 | 生命周期、隔离和删除传播契约/安全验证。 |
| V0.5-T03 | V0.3-T02,V0.5-T02 | 完成 RBAC/ABAC、职责分离、动态工具暴露和输入/RAG/Tool/输出 Guardrail。 | 默认拒绝、写前重新鉴权和投毒攻击安全验证。 |
| V0.5-T04 | V0.5-T01,V0.5-T03,V0.4-T03 | 建立 tool-based supervisor 与至少两个专职 Subagent，约束 handoff、allowlist 和循环上限。 | 路由、权限不放大、失败回收的契约与 E2E-F 验证。 |
| V0.5-T05 | V0.5-T04,V0.4-T04 | 建立 single/multi 等额预算、随机顺序、隐藏架构标签的公平对照 harness。 | 预注册 rubric，分别报告质量、成本、延迟与方差。 |
| V0.5-T06 | V0.5-T02,V0.5-T03,V0.5-T04 | 执行 Community/Security 场景并更新威胁模型与 Memory 保留/删除说明。 | Core 报告覆盖删除、投毒、权限和跨会话生命周期。 |
| V0.5-T07 | V0.5-T05,V0.5-T06 | 执行 single/multi 必需 Live 对照，历史场景 B Live 仅作证据引用。 | Live 报告须如实记录通过、失败或阻塞及质量/成本/延迟/方差。 |

## 验证分层说明

- **Fixture（F）**：使用 MockLLM、FixtureEmbedder、Mock Tool 和固定数据，证明控制流、类型契约、错误处理与确定性回归；不能证明真实模型或外部服务。
- **Community（C）**：使用本地 BGE、Chroma、SQLite、真实进程或网络回环，证明社区版本地链路、恢复和数据一致性；不能证明企业规模或企业系统兼容性。
- **Live（L）**：调用明确记录的真实公开模型 API，只证明该日期、模型和配置下的调用与质量结果；不能外推到其他模型或未来版本。
- **Enterprise（E-like/E）**：E-like 使用本地 PostgreSQL、Milvus、Redis、OTel 等企业栈组件，E 使用真实企业环境与 Adapter；两者均按目标独立验证，不能互相或由 Mock 替代。

当前已验证到：V0.1/V0.2 Core 与各自必需 DeepSeek Live 卡通过；V0.3 仅完成 Fixture/Community Core，使用 SQLite、官方 AsyncSqliteSaver、Mock 企业 Adapter 和合成数据，V0.3-T09 DeepSeek Live 尚未执行；V0.4 仅 T01 合成数据与标签隔离通过。真实企业 Adapter、E-like 多 worker、V0.4 动态归因 Live 和 V0.5 单/多 Agent 对照均未验证。
