# Oria · 企业级 Agent 平台架构设计

招商 B 端企业级 Agent 平台。吸收主流 Agent 框架成熟模式（LangGraph / DeepSeek Harness / Anthropic patterns / MCP），在 DAG 工作流 + Checkpoint + HITL + 审计之上，加 RAG 知识层、多智能体编排、上下文治理，跑通完整招商活动自动化。

> **定位**：简历旗舰项目①（招商领域 + 真实生产参考 + 开源自建）。**覆盖范围**：LangGraph 工作流 / Checkpoint / HITL / Eval / RAG 全链路 / 多智能体 / 上下文压缩 / MCP / Guardrails / 流式 / 异步长任务。领域逻辑、适配层、评测集与集成代码均在本仓库实现；执行与协议层复用 LangGraph、MCP SDK 等成熟基础设施，不复用任何外部 demo 项目代码。

---

## 零、实施交接（给实现方 Codex）

> 本节是架构作者给“无对话上下文的实现方”的交接说明，确保对方能准确起步、不偏航。本节与其余各节冲突时，以其余各节为准；本节只负责“如何开始”与“口径澄清”。

**0.1 当前状态（2026-08-28）**：架构已完成场景 A 资深 Agent 架构复核及工程路线修订；仓库已完成 V0.1-T01–T06，其中 T02/T03/T04/T05 经 remediation。T05 交付 ObjectStore 原文、SQLite catalog、按 embedding projection 隔离的 Chroma 投影、AuthorizedRetriever、可重建 citation 与 tenant-qualified `CampaignRuleSnapshot`，并补齐跨投影删除与失败重试；T06 交付 `search_campaign_rules`、`query_merchants` 与启动期封存的 ToolRegistry，执行参数/结果 schema、allowlist、授权、trust metadata 和受限资格输入脱敏。仓库已有锁定的 `uv 0.12.6 + Python 3.11`、`pyproject.toml`、`uv.lock`、`src/oria/` 与测试（145 passed）。V0.1-T07 及后续尚未开始，V0.1 Core Gate 与 Live 卡均未通过；真实 DeepSeek/BGE 未运行。第一里程碑仍是 V0.1 MVP（见 0.6），先交付永久保留的纵向切片，再逐层增强。**本文档与 `docs/Oria详细执行路线.md` 共同构成执行契约**；ADR 状态以 `docs/adr/README.md` 为准，冲突时必须先修正文档或 ADR，不能自行择一。

**0.2 实现方角色**：由 Codex 按 §九版本路线和 `docs/Oria详细执行路线.md` 实现。每次任务前先检查详细路线的准入、真实场景和测试；遇设计空白在 `docs/adr/` 建 ADR 并标“待 review”，不得擅自偏离架构。每条 ADR 对应 §十面试 hook。

**0.3 阅读路径**：§一-二理念 + 总体图 → §三模块职责 → §四 Provider 接口 → §五双场景 → §六双轨制 → §七数据库 → §八目录 → §九版本路线 → `docs/Oria详细执行路线.md` 的任务/验证 → §十 ADR → §十二插件层。

**0.4 “版本分层”不等于删减范围**：V0.1 只定义最早可运行版本，§九 V0.2–V0.8 的双场景、多智能体、HITL、权限、Durable Job、MCP、插件、迁移、Web UI 等仍全部交付。每个版本必须建立在上一版本的永久代码上，禁止先写 throwaway loop 再重写。深度分级只决定面试讲解重点，不决定是否构建。

**0.5 技术栈基线（实现方必须遵守）**：

- **Python 3.11**（与 CI `.github/workflows/ci.yml` 一致，为项目基线；本地 3.12 仅开发用，不作基线）。
- **依赖管理**：`uv + pyproject.toml`；每个版本新增依赖必须写入 `pyproject.toml` 并锁入 `uv.lock`。默认构建 = 社区版；正式版 backend 按 `edition`（§4.3）+ Protocol factory + ADR 切换。
- **依赖分组**：默认 dependencies 只放 `community+demo` 必需运行时；`standard` extra = sentence-transformers，本地模型另锁 revision；`eval` = BM25/reranker/RAG Eval；`postgres / redis / milvus / otel` 分别对应可选后端；`dev` dependency group 只放 ruff/mypy/pytest 等工具。MCP 自 V0.7 起是平台默认协议能力，不藏进 production-only extra。CI 每个 extra 独立建 job，防止“all extras 一起安装才偶然可用”。
- **双轨与运行 profile**：`edition=community|production` 表示产品轨道；`runtime_profile=demo|standard` 表示运行预设。`community+demo` 必须零账号、零 Key、零外部服务可跑；`community+standard` 使用用户自带 LLM Key + 本地组件；`production` 只允许 `standard` 并 fail closed。
- **按里程碑引入**：
  - **V0.1**：core/CLI/test 工具链 + `typer`、`pydantic-settings`、`pyyaml`、`jinja2`、`structlog`、`sqlalchemy + alembic + aiosqlite`、`langgraph`、`langgraph-checkpoint-sqlite`、`chromadb`、`httpx`。从 Merchant 与最小知识 catalog 首表就分别建立 business/platform migration，避免 V0.2/V0.3 重做存储；`sentence-transformers` 放入 `standard` optional extra，避免零配置 demo 被模型运行时拖重；从第一版即使用 LangGraph，不写一次性 loop。
  - **V0.2**：`rank-bm25`、reranker 与 `eval` optional extra 中的 `ragas`，完成混合检索、RAG Eval，并先落 `Principal + PolicyEngine` 的 tenant/document read ACL；没有授权决策不得声称 ACL 完成。
  - **V0.3–V0.5**：V0.3 在 V0.1 的 Alembic 基线上追加 ProductSnapshot/CampaignRuleSnapshot/Campaign/CouponBatch/LaunchSagaState/RecruitmentPublication/EnrollmentItem/ConfirmationTask/AssortmentSubmission/SelectionDecision/ConsumerPlacement/Notification、写操作 RBAC、职责分离与审批复核，不重建业务 DB。V0.5 再完成 ABAC/Guardrails。Supabase adapter、多智能体与 Memory 依赖随后按实现锁定。
  - **V0.6**：`fastapi + uvicorn + psycopg + langgraph-checkpoint-postgres`；社区单 worker 可继续 SQLite，双 worker/lease 企业语义必须在 PostgreSQL 容器验证，不依赖 FastAPI 进程内 background task。
  - **V0.7–V0.8**：官方 `mcp>=2,<3` SDK、`opentelemetry-sdk + OTLP exporter`，选装 `pymilvus / redis / langfuse`；沿用 V0.1 已固定的结构化日志字段，不到后期更换日志模型。
- **社区版默认**：Chroma、本地 Embedder、SQLite（平台/Checkpoint/招商种子数据）、内存缓存、本地对象目录、Console JSON、Mock IM。Supabase、OTLP Collector 和真实 IM 均为显式可选项，不是快速开始前置条件。

**0.6 V0.1 MVP 交付物与 Definition of Done**：

- **Hero 场景**：场景 A 只读 MVP——“解析招商需求 → 检索并结构化活动规则 → 确定性预筛样本商家 → LLM 对软条件排序并输出活动/券批次草案预览、推荐理由与逐字段引用”。只使用 `search_campaign_rules`（RAG）和 `query_merchants` 两个只读工具，不创建任何业务对象；V0.3 在同一 Graph 上追加活动与券批次草案、运营审核、招商投放、报名/商品圈选、券关联、招后选品、C 端投放和商家通知。
- **永久骨架**：src layout、进程级 RuntimeServices/每次执行 Context/Protocol、单租户本地 Principal/PolicyEngine seam、官方 `AsyncSqliteSaver`、CLI、tests、README；使用自有 `LLMProvider/Tool` 构建永久 LangGraph `StateGraph` 有界研究子图（model → tools → validate，工具 allowlist + 最大步数），禁止依赖已弃用的 `langgraph.prebuilt.create_react_agent` 或另写随后废弃的 loop。
- **LLM**：首批实现 `MockLLMProvider + OpenAICompatProvider(DeepSeek Responses-dialect profile)`，不写随后被合并的专用 DeepSeek 类；类型化 stream/tool-call/structured-output 契约不降级。其余 OpenAI-compatible profile 与 AnthropicProvider 在 V0.2 补齐，V0.7 再验证外部 Provider 插件。
- **RAG**：随仓库提供脱敏招商规则、样本商家和 deterministic fixtures；Chroma + `Embedder` seam。demo profile 可用 FixtureEmbedder/预置 fixture，standard profile 使用本地 BGE。
- **双 profile**：`uv run oria demo` 自动初始化并以 `community+demo` 零配置跑通；`ORIA_RUNTIME_PROFILE=standard ORIA_LLM_PROFILE=deepseek DEEPSEEK_API_KEY=... uv run oria demo` 切真实 LLM，其余仍用本地组件。
- **可见输出**：结构化需求与规则快照摘要、活动/券批次草案预览、推荐商家及理由、逐字段文档引用、两个工具调用轨迹、provider/profile、耗时；不得显示隐式思维链、黑白名单原文或未脱敏参数。
- **Core DoD（允许进入 V0.2）**：两种 profile 使用同一 Graph/Protocol；demo 确定性 CI 与社区本地链路通过；构建 wheel 后在无源码路径的空目录仍能初始化 migration/resources 并启动；无 Supabase/Redis/Milvus/OTLP/IM 账号也能运行；目标 `ruff + mypy + pytest` 命令通过。
- **Live DoD（允许声明“真实模型 MVP 已验证”）**：真实 DeepSeek + 锁定 BGE smoke 通过并保存 request ID、model/revision、usage 和报告。缺 Key 不阻塞后续代码实施，但 V0.1 状态必须是“Core 完成，Live blocked/待验证”，不得对外合并成“V0.1 全部通过”。
- **首次跑通目标**：以 `docs/Oria详细执行路线.md` 的 V0.1 关键路径为准；“首个 fixture demo”与“稳定 Live demo”分别计时，只报告实际完成时间，不把 1 天或 2–3 天写成承诺。

**0.7 场景 A 资深 Agent 架构评审记录（2026-08-26，设计已同步，实现/验证未开始）**：业务 10 步流程对原设计有实质影响，已作以下收敛：① 把 RAG 输出从松散文档片段升级为带逐字段引用的不可变规则快照；② 把硬资格过滤从 LLM 移到确定性 EligibilityPolicy，LLM 仅做候选集内软排序、草案和解释；③ 将含糊的“投放”拆成商家侧招商投放与 C 端投放；④ 增加商家自主报名/系统自动圈品的并行汇聚、动态业务确认链、报名商品—券批次关联和异步招后选；⑤ 扩展领域模型、Tool、状态机、Outbox/对账与验证门禁。V0.1 仍保持两个只读工具和零副作用，以 `CampaignProposal` 预览贯穿到 V0.3，不以缩短 MVP 为由另造一次性流程。

**0.8 复核缺口闭环（2026-08-27，设计已修正，代码/验证未开始）**：进一步补齐 `ProductSnapshot + ProductEligibilityPolicy + product_circle_policy_ref/version`，并将自动圈品规则与招后 `assortment_policy_ref/version` 分离；同时冻结 `merchant|auto|hybrid` 关窗/join 语义、`InboundRequest/IntegrationEventEnvelope` 事件 union、ToolPolicy `approval_mode` 决策矩阵与 LaunchPlan saga 部分成功后的补偿/对账语义；ADR-028/029 已实体化，其余 ADR 的实体化时点由索引约束。

---

## 一、设计理念

Oria 是企业级 Agent 平台，吸收主流 Agent 框架的成熟模式，不重新造轮子。核心五条原则：

| # | 原则 | 借鉴来源 | 落地 |
| --- | --- | --- | --- |
| 1 | 两层模型：执行层单一 spine + harness 插件层 | LangGraph（执行）+ DeepSeek Harness（薄核心 / 插件） | `orchestrator/` 用 LangGraph StateGraph；`core/` = Context + EventBus + 中间件 + Protocol |
| 2 | Workflow 与 Agent 两条主线 | Anthropic《Building Effective Agents》 | 预定图（Workflow，场景 A）+ ReAct 条件图（Agent，场景 B）；内置 5 种模式（routing / parallelization / orchestrator-workers / evaluator-optimizer / prompt-chaining） |
| 3 | 能力即可替换插件 | DeepSeek Harness（seam / 可逆效应）+ MCP（外部工具协议） | Provider / Tool / Retriever / Memory / Guardrail 注册到 Context；MCP 与内部插件分层 |
| 4 | 安全与可观测横切 | OpenAI Agents SDK（Guardrails / Tracing） | `guardrails/ + obs/`（OTel）+ `eval/` + 成本预算门禁 |
| 5 | 运行时与领域事件分离 | LangGraph Checkpointer + Transactional Outbox | Checkpoint 是执行恢复真相源；Domain/Audit Event 是业务与审计真相源；事件在所属 DB 与源状态同事务提交，禁止跨库双写 |

> **何时用 Workflow、何时用 Agent（ADR-006）**：流程已知、跨天、需持久化编排 → 预定图（Workflow）；探索性、实时、依赖中间发现 → ReAct Agent。Oria 两类都支持，场景 A / B 分别示范。

> **两层模型补充**：执行层从 V0.1 起统一使用 LangGraph，同时承载场景 A（MVP 简约图 → 完整预定 DAG）与场景 B（ReAct 条件图），复用 Checkpoint / HITL / 多智能体，不自研图引擎；`core/` harness 层的 turn-flow 中间件经 LangGraph callback 包裹节点执行，不存在需要迁移的第二套 loop。

---

## 二、总体架构

```text
┌────────────────────── 接入层 ──────────────────────┐
│ CLI / Web UI / 飞书·钉钉 Webhook → Ingress Adapter │
│ → API（验签、AuthN/AuthZ、限流、SSE、幂等键）       │
└──────────────────────────┬──────────────────────────┘
                           ▼
┌──────────────── Job Control Plane ─────────────────┐
│ Durable Job State Machine / Worker Lease / Retry   │
│ Cancel / HITL / External Event Wait / Webhook      │
└──────────────────────────┬──────────────────────────┘
                           ▼
┌──────────────────── Agent Runtime ──────────────────┐
│ LangGraph Workflow / ReAct / Multi-Agent / State    │
│ Planner + Context Governance + Memory-as-Tool       │
│                                                     │
│        ┌──────────── model ↔ tool loop ───────────┐ │
│        │ LLM Provider ↔ Tool Router              │ │
│        │                  ├─ RAG / Knowledge      │ │
│        │                  ├─ 招商 Domain / DB     │ │
│        │                  ├─ IM / Raptor / Watson │ │
│        │                  └─ MCP                  │ │
│        └──────────────────────────────────────────┘ │
└──────────────────────────┬──────────────────────────┘
                           ▼
┌────────────────────── 数据平面 ─────────────────────┐
│ Checkpoint DB / Domain DB / Outbox+Audit / Vector  │
│ Redis / Object Storage                             │
└─────────────────────────────────────────────────────┘

横切控制：Policy（tenant/RBAC/ABAC）│ Guardrails │ Secrets
          OTel Trace/Metrics/Logs   │ Eval       │ Cost Budget
```

---

## 三、模块职责

| 模块 | 职责 | 关键技术 / 约束 |
| --- | --- | --- |
| `core/` | 薄核心：RuntimeServices / Context / EventBus / 中间件 / Protocol / 注册表 | 进程级资源与每次执行身份分离、可逆效应、typed 事件三域、turn waterfall、entry-point 插件发现 |
| `providers/` | LLM 抽象层，5 家厂商 profile | OpenAI 兼容（4 家配置驱动）+ Anthropic；capability、标准错误、类型化 streaming |
| `prompts/` | Prompt 版本管理 | Jinja 模板 + 目录版本（够用级，不做 A/B 平台） |
| `tools/` | 工具注册 / 调度 + MCP | Function calling、并行调用、JSON Schema、结构化输出强约束、工具路由（embed 描述召回 top-k） |
| `memory/` | 短期对话 + 长期记忆 + 上下文治理 | 滑窗摘要、向量情景记忆、预算压缩 + 事实账本；memory-as-tool（`save/search_memory`）、用户级跨会话记忆 |
| `rag/` | 知识库摄入 / 检索 | Chunking、混合检索（dense + BM25）、rerank、RAGAS；pre-filter（按用户权限 metadata 过滤再召回） |
| `agent/` | 有界 Agent 子图 + Planner | V0.1 用 StateGraph 自建 model/tools/validate 条件循环；场景 A 将其作为研究子图，场景 B 复用并扩展动态工具选择 |
| `orchestrator/` | 工作流引擎 + 执行原语 | 基于 LangGraph StateGraph：预定 DAG（场景 A）、自动 / 人工节点、`on_failure`、Checkpoint、HITL；5 种模式作 builder 辅助构造（非重造 LangGraph 接线） |
| `domain/` | 招商领域模型与不变量 | Merchant / ProductSnapshot / CampaignRuleSnapshot / Campaign / CouponBatch / LaunchSaga / RecruitmentPublication / EnrollmentItem / AssortmentSubmission / SelectionDecision / ConsumerPlacement / Notification；唯一约束、状态机与事务边界 |
| `storage/` | 后端实现与连接生命周期 | SQLAlchemy Repository、Chroma/Milvus、CacheStore、ObjectStore；只由 Factory 创建，业务层依赖 Protocol |
| `permission/` | tenant + RBAC + ABAC + 职责分离 | 唯一 PolicyDecision 来源；actor × executor × action × resource/data 权限；deny-by-default、policy version、RAG 强制 ACL |
| `guardrails/` | 安全横切层 | 输入检测（Prompt 注入 / 越狱）+ 输出过滤（PII / 毒性）+ 工具权限复核 |
| `obs/` | 可观测 + 成本核算 | Trace（OTel span：LLM / Tool）、token / 成本、metrics；预算门禁的核算在此，执行路径（middleware pre-step）校验超限 → raise interrupt 转 HITL |
| `eval/` | 评估（一等子系统） | 场景基线数据集 + 回归 CI 门禁 + LLM-as-judge + 指标（任务成功率 / 工具准确率 / 幻觉率） |
| `ingress/` | 多入口请求规范化 | CLI / 飞书 / 钉钉消息统一为 CampaignIntent；webhook 验签、防重放、消息去重和可信主体映射 |
| `api/` | 对外服务 | FastAPI v1、AuthN/AuthZ、幂等提交、SSE 续传、审批与 Job 查询/取消 |
| `jobs/` | 持久化长任务控制面 | DB 状态机、worker lease / heartbeat、重试、取消、HITL 等待、webhook 投递 |
| `cli.py` | 开发入口 | 便于联调 |
| `web/` | 企业工作台 | React + TypeScript + Vite；OpenAPI 生成 client；提交、SSE 轨迹、审批、取消与结果页；Playwright E2E |

> **权限单一来源**：`permission/` 是唯一鉴权源，RAG pre-filter 与 Guardrails 工具权限复核均调用它，不另实现。**评估单一 harness**：`eval/` 是统一评估框架，RAGAS / LLM-as-judge / 任务成功率都是注册进来的 eval 方法，非分散实现。

> **可执行 / 可测试前提**：① 企业系统 Adapter（商家/商品库、券批次、商家侧招商、报名、选品、C 端投放、IM 入站/通知，以及 DMS / Raptor / Watson）在 OSS 仓库提供 Mock 实现（`tools/builtin/_mock/` 与 `ingress/_mock/`），真实接入层单独隔离，保证对外可跑可演示；② `providers/` 内置 `MockLLMProvider`（固定响应 / 录放），CI 确定性测试免 key 免费。

> **MockLLMProvider 录放模式**：请求哈希包含 provider/model/prompt/tool schema+version/response schema+structured mode；录制前脱敏，回放只用于确定性契约回归，不冒充当前真实模型质量。每个 Hero 场景初始至少 30 条人工审阅 golden，以其建立确定性 PR baseline；首次真实 eval run 另建 Live 质量基线，完成重复采样/人工校准后才设置 Live 回归阈值。cost / latency 独立评估。

### 3.1 接口契约

各服务的 `typing.Protocol` 定义在 `src/oria/core/protocols.py`，实现方必须遵守。统一约定：I/O 方法全部 async 并传每次执行的 `ctx: Context`（纯函数式 metadata/render helper 可同步）；唯一例外是处于 actor 建立之前的 `IngressAdapter`，它使用只含受信 executor/request/correlation 的 `IngressContext`，验证并映射主体后才允许创建普通 Context。返回用 Pydantic v2 model、dataclass 或 TypedDict，不用裸 dict。`RuntimeServices` 不包含任何 actor/session/run 状态，`Context` 不拥有连接池和 teardown，二者都不作为可序列化 WorkflowState 的字段。未列字段以 §三模块职责表与 §四为准。

```python
# 进程级资源：API/worker 在 lifespan 只构建一次；CLI 每次调用构建一次
class RuntimeServices:
    config: ResolvedRuntimeConfig # 启动时解析并冻结；运行中不再读取 env/config file
    llm: LLMProvider
    tools: ToolRegistry
    retriever: Retriever
    embedder: Embedder
    memory: Memory
    guardrails: GuardrailRegistry
    nodes: NodeRegistry
    agents: AgentRegistry
    policy: PolicyEngine           # 唯一鉴权决策源
    domain: DomainServiceRegistry # Tool 只调用领域 Service；Repository 不直接暴露给 Agent/Graph
    cache: CacheStore
    objects: ObjectStore
    ingress: IngressRegistry   # CLI/飞书/钉钉入站规范化；真实 webhook 由 API 调用
    notifier: NotifierRegistry  # IM 通道（大象/飞书/钉钉，可插拔，见 §3.5）
    _exit_stack: AsyncExitStack # 只由 build_runtime/启动期插件 setup 持有并统一 teardown


# 每次请求/CLI run/Job attempt 新建，冻结后注入每个方法
@dataclass(frozen=True)
class Context:
    runtime: RuntimeServices
    actor: Principal            # 发起/被代表的业务主体；异步恢复时从可信主体目录重新加载
    executor: Principal         # 当前 API/CLI/worker/MCP workload 身份，与 actor 分开授权/审计
    session_id: str
    thread_id: str              # LangGraph 持久化游标；一个 session 可含多个 thread
    run_id: str                 # 单次执行 ID
    job_id: str | None          # 异步任务 ID；同步调用为空

    # ctx.llm / ctx.tools / ctx.policy / ctx.domain 等是只读转发属性，
    # 方便 Protocol 消费者使用，不复制或修改 runtime 资源。


# LLM（完整见 §四）
class LLMProvider(Protocol):
    async def chat(
        self,
        messages: list[Message],
        ctx: Context,
        tools: list[ToolSpec] | None = None,
        options: ChatOptions | None = None,
    ) -> ChatResult: ...

    async def chat_stream(
        self,
        messages: list[Message],
        ctx: Context,
        tools: list[ToolSpec] | None = None,
        options: ChatOptions | None = None,
    ) -> AsyncIterator[StreamEvent]: ...

    async def capabilities(self, ctx: Context) -> ProviderCapabilities: ...

# StreamEvent 使用 discriminator：TextDelta / ToolCallDelta / ReasoningDelta /
# UsageDelta / Done / ProviderError；未知 provider event 保留 raw_type/raw_payload。


# PolicyEngine：唯一授权决策源；actor 与 executor 必须来自可信认证/Job 上下文，
# 不接受模型、RAG、Tool 参数或客户端 header 自报角色。
class PolicyEngine(Protocol):
    async def authorize(
        self,
        request: AuthorizationRequest,
        ctx: Context,
    ) -> PolicyDecision: ...


# Tool
class Tool(Protocol):
    name: str
    schema_version: int
    description: str  # 工具路由 embed 召回的描述文本
    json_schema: dict  # 参数 JSON Schema（function calling 用）
    result_schema: dict # ToolResult.data 的模型可见投影 schema
    policy: ToolPolicy # 风险、副作用、超时、重试、幂等与审计策略

    async def run(self, params: dict, ctx: Context) -> ToolResult: ...

# ToolResult 完整 schema 见下方值类型；外部异常不得直接作为字符串泄露给模型，
# 外部数据必须携带 trust/provenance/classification 后才能进入模型上下文。
# ToolRegistry 支持 embed 描述召回 top-k（router.py）


# Retriever（RAG）
class Retriever(Protocol):
    async def retrieve(
        self,
        query: str,
        ctx: Context,
        k: int = 5,
        query_filters: QueryFilters | None = None,
    ) -> list[Doc]: ...

# Doc 含 tenant/version/source/ACL/provenance，完整 schema 见下方值类型。
# 调用方只能传业务 query_filters；AuthorizedRetriever 必须通过 ctx.policy 计算
# ACLFilter，并与 query_filters 做 AND。调用方不能覆盖/清空 tenant 与 ACL 条件。


# Embedder
class Embedder(Protocol):
    dim: int

    async def embed(self, texts: list[str], ctx: Context) -> list[list[float]]: ...


# Memory（短期对话 + 长期向量 + 上下文治理；memory-as-tool 见 memory_tool.py）
class Memory(Protocol):
    async def load(self, ctx: Context) -> list[Message]: ...  # 短期对话历史
    async def append(self, msg: Message, ctx: Context) -> None: ...
    async def search(
        self,
        query: str,
        ctx: Context,
        k: int = 5,
    ) -> list[MemoryItem]: ...  # 长期向量记忆

    async def compress(self, ctx: Context) -> None: ...  # 预算压缩 + 事实账本（ADR-012）

# MemoryItem 含 subject/provenance/confidence/sensitivity/TTL，完整 schema 见下方值类型。


# Node（工作流节点；LangGraph node 适配）
class Node(Protocol):
    async def execute(self, state: WorkflowState, ctx: Context) -> NodeResult: ...

# NodeResult 只描述本次调用的 completed/failed/waiting 状态；批量、周期、常驻属于
# Job 的 SchedulePolicy，不放进 Node，避免执行语义与调度语义耦合（ADR-005）。

# WorkflowState 见 §3.2（Oria 状态 schema）


# Guardrail
class Guardrail(Protocol):
    phase: Literal["input", "output", "tool"]  # 输入检测 / 输出过滤 / 工具权限复核

    async def check(self, content: Any, ctx: Context) -> GuardrailResult: ...

# GuardrailResult = {
#     passed: bool,
#     reason: str | None,
#     action: Literal["block", "redact", "warn"],
# }


# Ingress / Notifier（IM 入站与出站，可插拔；见 §3.5）
class IngressAdapter(Protocol):
    name: str  # "cli" | "feishu" | "dingtalk"

    async def verify_and_normalize(
        self,
        request: InboundRequest,
        ingress_ctx: IngressContext,
    ) -> InboundMessage: ...


class Notifier(Protocol):
    name: str  # "daxiang" | "feishu" | "dingtalk"

    async def send_message(self, target: str, text: str, ctx: Context) -> SendResult: ...
    async def send_file(self, target: str, file_path: str, ctx: Context) -> SendResult: ...

# SendResult = {ok: bool, message_id: str | None, error: str | None}
```

**Registry vs Protocol**：ToolRegistry / GuardrailRegistry / NodeRegistry / AgentRegistry / IngressRegistry 是可扩展注册表（注册 + 可逆效应 + entry-point 发现），其元素实现对应 Protocol；`DomainServiceRegistry` 是 Factory 组装的固定类型化 Service 容器，不接受模型或运行时插件随意替换。IngressRegistry 仅在 actor 建立前由受信 CLI/API 边界配合 `IngressContext` 使用；其余消费方通过普通 Context 与 Protocol 取用能力。`ctx.policy` 是 actor 建立后的唯一鉴权决策源：AuthorizedRetriever、Memory、Tool Router/Executor、Guardrail（`phase="tool"`）和 API 都提交包含 actor + executor 的 `AuthorizationRequest`，不得各自解释角色或接受调用方提供 ACL。除显式、可审计的 delegation 外，request 中主体必须与可信 `ctx` 一致。

值类型（以下是 schema 摘要，不是可复制的 Python 语法；实现时必须建立对应 Pydantic v2 model / dataclass / TypedDict，并在契约测试中校验序列化）：

```python
Message        = {role: "system" | "user" | "assistant" | "tool",
                  content: str | list[ContentBlock], tool_call_id?: str}
ContentBlock   = TextBlock | ImageBlock | ToolCallBlock | ToolResultBlock |
                 CitationBlock | RefusalBlock | ProviderExtensionBlock
                 # Provider-agnostic；未知 provider block 可无损保留但不默认下发客户端
ChatResult     = {content: list[ContentBlock], tool_calls: list[ToolCall],
                  structured_output: dict | None, usage: Usage,
                  finish_reason: str | None, request_id: str | None,
                  refusal: str | None, raw_response: dict | None}
ToolCall       = {id: str, name: str, args: dict}
ToolSpec       = {name: str, schema_version: int, description: str,
                  json_schema: dict, strict: bool}
                 # 下发给 LLM 的工具定义
ChatOptions    = {temperature: float | None, max_output_tokens: int | None,
                  tool_choice: str | dict | None, parallel_tool_calls: bool | None,
                  response_schema: ResponseSchema | None, timeout_seconds: float | None}
ResponseSchema = {name: str, json_schema: dict, strict: bool}
ProviderCapabilities = {tool_calling: bool, streaming: bool, reasoning: bool,
                  structured_output: bool, parallel_tool_calls: bool,
                  structured_output_modes: set[Literal["native_json_schema", "synthetic_tool"]],
                  api_dialect: Literal["mock", "chat_completions", "responses", "anthropic_messages"],
                  multimodal_inputs: set[str], context_window: int | None,
                  max_output_tokens: int | None}
Usage          = {input_tokens: int, output_tokens: int, reasoning_tokens: int | None,
                  cache_read_tokens: int | None, cache_write_tokens: int | None,
                  cost: float | None}
ToolPolicy     = {risk_level: "low" | "medium" | "high", side_effect: bool,
                  timeout_seconds: float, retry_policy: RetryPolicy,
                  idempotency_scope: str | None, required_action: str,
                  resource_type: str, redact_fields: list[str],
                  approval_mode: "none" | "conditional" | "required",
                  approval_action: str | None,
                  business_confirmation: bool}
ToolResult     = {ok: bool, data: Any, error: ToolError | None,
                  execution_id: str, idempotency_key: str | None,
                  trust_level: str, provenance: str,
                  data_classification: str}
ToolError      = {code: str, safe_message: str, retryable: bool,
                  details_ref: str | None}
AgentTermination = {reason: str, limits: dict, observed_usage: dict,
                  last_safe_evidence_refs: list[str]}
StreamEvent    = TextDelta | ToolCallDelta | ReasoningDelta | UsageDelta | Done | ProviderError
                 # 均有 type discriminator、sequence、provider/model/request_id
Doc            = {id: str, version: str, tenant_id: str, content: str,
                  metadata: dict, score: float, source_uri: str, acl: ACLMetadata}
MemoryItem     = {id: str, tenant_id: str, subject_id: str, content: str,
                  provenance: str, confidence: float, sensitivity: str,
                  expires_at: str | None, score: float}
Principal      = {subject_id: str, tenant_id: str, kind: "human" | "service",
                  roles: list[str], attributes: PrincipalAttributes, authn_method: str}
AuthorizationRequest = {actor: Principal, executor: Principal,
                  action: str, resource: ResourceRef,
                  context: AuthorizationContext}
PolicyDecision = {allow: bool, constraints: dict, policy_version: str, reason: str}
Plan           = {goal: str, steps: list[Step]}  # Planner 拆解
Step           = {node_id: str, params: dict}
RunMeta        = {tenant_id: str, session_id: str, thread_id: str,
                  run_id: str, job_id: str | None,
                  requester_subject_id: str}  # 创建后不可变；不持久化角色或长期凭证
HitlState      = {approval_id: str, step_id: str, tool_name: str,
                  args_hash: str, checkpoint_id: str, policy_version: str,
                  requested_by: str, requested_at: str, expires_at: str,
                  resolved_by: str | None, resolved_at: str | None,
                  decision: "approve" | "reject" | None}
ExternalWaitState = {wait_id: str, step_id: str, event_type: str,
                  resource: ResourceRef, expected_version: str | None,
                  checkpoint_id: str, correlation_token_hash: str,
                  requested_at: str, expires_at: str,
                  timeout_action: "resume" | "fail" | "cancel",
                  resolved_event_id: str | None, resolved_at: str | None}
NodeResult     = {status: "completed" | "failed" | "waiting", updates: dict,
                  error: NodeError | AgentTermination | None}
SchedulePolicy = {mode: "once" | "batch" | "periodic" | "daemon",
                  batch_size: int | None, cron: str | None}  # ADR-005
InboundRequest = {headers: dict[str, str], raw_body: bytes, received_at: str,
                  request_id: str, remote_addr: str | None}
                  # raw_body 只用于验签/防重放，受大小上限约束，不写日志或 WorkflowState
IngressContext = {executor: Principal, request_id: str, correlation_id: str}
                  # pre-auth 边界；没有 actor/tenant/roles 自由输入，不进入 WorkflowState
InboundMessage = {source: "cli" | "feishu" | "dingtalk", source_message_id: str,
                  mapped_tenant_id: str, mapped_subject_id: str, sender_ref: str,
                  target_ref: str | None, text: str, received_at: str,
                  verified: bool, dedupe_key: str}
CampaignIntent = {request_text: str, requested_region: list[str],
                  requested_categories: list[str], target_count: int | None,
                  effective_at: str, source_message_id: str | None}
IntegrationEventEnvelope = {adapter_id: str, source_event_id: str,
                  event_type: "merchant.enrollment_upserted" | "enrollment.window_closed" |
                              "selection.decision_recorded" | "selection.completed",
                  mapped_tenant_id: str, resource: ResourceRef,
                  resource_version: str | None, occurred_at: str,
                  payload_hash: str, sanitized_payload: dict,
                  actor: Principal | None, executor: Principal,
                  verified_at: str, dedupe_key: str}
ProductSnapshot = {product_ref: str, product_version: str, merchant_id: str,
                  category: str, normalized_price: str, currency: str,
                  normalized_title: str, keyword_labels: list[str],
                  eligibility_facts: dict, captured_at: str, source_ref: str}
LaunchChildStep = {tool_name: str, canonical_args_hash: str,
                  idempotency_scope: str}
LaunchPlan     = {campaign_draft_id: str, campaign_draft_hash: str,
                  rule_snapshot_id: str, rule_snapshot_hash: str,
                  coupon_batch_draft_id: str, coupon_batch_draft_hash: str,
                  merchant_scope_hash: str, material_version: str,
                  child_steps: list[LaunchChildStep],
                  compensation_policy_version: str, plan_hash: str}
LaunchSagaState = {saga_id: str, campaign_id: str, version: str,
                  status: "planned" | "coupon_materialized" |
                  "recruitment_published" | "completed" |
                  "compensation_pending" | "reconciliation_required" | "failed",
                  launch_plan_hash: str, completed_step_ids: list[str],
                  last_receipt_ref: str | None, error_code: str | None}
AssortmentSubmission = {submission_id: str, campaign_id: str,
                  enrollment_item_ids: list[str], assortment_policy_ref: str,
                  assortment_policy_version: str, request_version: str,
                  status: "pending" | "accepted" | "completed" | "unknown"}
SelectionDecision = {submission_id: str, selection_version: str,
                  product_ref: str, product_version: str,
                  decision: "selected" | "rejected", reason_code: str,
                  occurred_at: str}
ConsumerPlacementDraft = {campaign_id: str, selection_version: str,
                  eligible_link_version: str, placement_spec: dict,
                  placement_hash: str}
Score          = {value: float, reason: str}      # evaluator-optimizer 评分
Task           = {id: str, description: str, params: dict}
                 # orchestrator-workers 分派单元
SendResult     = {ok: bool, message_id: str | None, error: str | None}
                 # IM 通道发送结果（见 §3.5）
```

`ToolResult` 判别式固定为：`ok=true` 时 `error=None` 且 `data` 必须通过 `result_schema`；`ok=false` 时 `data=None` 且 `error` 必填。稳定错误码至少包含 `unknown_tool / invalid_arguments / permission_denied / timeout / rate_limited / unavailable / execution_failed / side_effect_unknown / object_store_failed`，adapter 只能补充新码，不能把原始异常文本当错误码或 safe message。

`WorkflowState` 见 §3.2。所有类型用 Pydantic v2 BaseModel 或 TypedDict；RuntimeServices 持有各 Registry，Context 只持有 runtime 引用和本次执行不可变 metadata，Registry 元素实现对应 Protocol。

### 3.2 状态 / Checkpoint / Domain Event / Audit 四者关系（ADR-004 / ADR-018）

> **定稿**：执行恢复与业务审计采用不同但可关联的真相源。LangGraph 官方 Checkpointer 保存执行状态、channel version 与 pending writes，是 resume / time-travel 的唯一依据；Business DB 的 `domain_events` 保存领域事实；Platform/Business DB 各自的 `audit_events` 保存本库操作的安全审计事实。可持久事件与所属聚合在同一 DB 事务中写入本库 outbox，禁止为了“统一事件表”做 platform/business 跨库双写；不从精简事件反推 LangGraph 内部状态。

**WorkflowState（Oria 的 LangGraph 状态 schema，TypedDict）**：

```python
from typing import Annotated
from langgraph.graph.message import add_messages

class WorkflowState(TypedDict):
    messages: Annotated[list[Message], add_messages]
    plan: Plan
    # 并行分支只写自己的 node_id；merge_results 必须满足结合律并拒绝同 key 冲突
    results: Annotated[dict[str, NodeResult], merge_results]
    # 支持并行分支分别等待审批；approval_id 重复但内容不同即报冲突
    approvals: Annotated[dict[str, HitlState], merge_unique]
    # 报名窗口、选品回执等外部等待；事件必须先过 inbox 去重/鉴权再恢复
    external_waits: Annotated[dict[str, ExternalWaitState], merge_unique]
    meta: RunMeta            # 只读关联标识，节点不得更新
```

`merge_results` 与 `merge_unique` 使用同一“只允许新增或幂等重放”的合并语义。比较前把 Pydantic/dataclass 值转成确定性的 JSON mode；不得靠对象地址或 `repr` 判断相等。以下伪代码是实现契约，而非示意性建议：

```python
class StateConflictError(RuntimeError): ...

def merge_unique_map(left: dict[str, T], right: dict[str, T]) -> dict[str, T]:
    merged = dict(left)  # 不原地修改任一输入
    for key, incoming in right.items():
        if key not in merged:
            merged[key] = incoming
        elif canonical_json_value(merged[key]) != canonical_json_value(incoming):
            raise StateConflictError(key)
        # 同 key、同内容是 checkpoint/replay 的幂等重放，保留原值
    return merged

merge_results = merge_unique_map
merge_unique = merge_unique_map
```

该 reducer 在无冲突输入域上满足结合律与交换律；相同更新可重复应用。发现同一 `node_id`、`approval_id` 或 `wait_id` 的不同内容时必须抛出 `StateConflictError`，使当前 super-step/join 失败，禁止 log 后择一、last-write-wins 或吞掉分支。官方 saver 保留上一个已接受 checkpoint 与 pending-write 语义；修正冲突 ID/分支设计后才能重新执行或恢复。Trace 只记录冲突 key、run/checkpoint 关联和异常类型，不记录敏感 value。

除 `messages/results/approvals/external_waits` 外，任何可能被并行分支写入的 key 都必须定义结合律 reducer；否则使用分支私有 state 并在显式 join 节点合并。当前节点由 LangGraph task/checkpoint 表示，不在共享 state 维护易冲突的 `step_id`。成本从 Usage/Event 聚合，不在并行 state 中累加。禁止节点原地修改传入 state。

**DomainEvent / AuditEvent（append-only）**：

```python
class EventEnvelope(TypedDict):
    event_id: str
    schema_version: int
    event_type: str
    tenant_id: str
    session_id: str
    thread_id: str
    run_id: str
    job_id: str | None
    actor: Actor
    action: str
    resource: ResourceRef
    correlation_id: str
    causation_id: str | None
    idempotency_key: str | None
    payload: dict
    data_classification: Literal["public", "internal", "confidential", "restricted"]
    created_at: str
```

**Checkpoint 适配**（`orchestrator/checkpoint.py`）：

- 社区版直接封装官方 `AsyncSqliteSaver`，正式版封装官方 `AsyncPostgresSaver`；Oria adapter 只负责补 tenant/run metadata、serializer 加密策略和 trace，不重新实现 saver 语义。
- 必须覆盖安装版本 `BaseCheckpointSaver` 的完整 async 契约，包括 `aput`、`aput_writes`、`aget_tuple`、`alist`；不得以 `aget` 代替 `aget_tuple`。
- API/领域层使用不可猜的 external `thread_id`；Checkpoint adapter 必须从已鉴权 `tenant_id + external_thread_id` 生成 tenant-qualified `storage_thread_id`：固定使用 `v1` 版本的长度前缀 + UTF-8 + base64url 编码，不使用有分隔符碰撞的裸拼接或需要密钥轮换的临时 HMAC 方案。再将 storage key 传给官方 saver，并在 metadata 中保留 tenant/external ID 供校验。不得把用户传入的 thread_id 直接作为 saver key，也不对 API 暴露 storage key；两个 tenant 即使 external thread_id 相同也必须完全隔离。
- `checkpoint_ns/checkpoint_id/parent checkpoint/channel_versions/versions_seen/pending writes/metadata` 均由 saver 原样保留。
- serializer 禁止反序列化不可信 pickle；敏感 checkpoint 加密并执行保留/删除策略。
- 自研 `EventSourcedCheckpointer` 作为 V0.8 扩展实验保留：只有通过官方 saver compatibility suite、并发与故障注入测试后才能切换，不能作为默认实现。

| 数据 | 定位 | 用途 |
| --- | --- | --- |
| Checkpointer 表 | 执行状态真相源 | resume / pending writes / time-travel |
| Business `domain_events` | append-only 业务事实 | 业务回放、投影、对账 |
| Platform/Business `audit_events` | 两库各自 append-only、访问受控的审计事实 | 身份/API/Job/审批与业务副作用分库追踪 |
| Platform/Business `outbox` | 与本库源状态同事务的待投递事件 | 保证单库状态与事件一致，消费者幂等投递 |

> **resume 流程**：以 `(tenant_id, thread_id, checkpoint_ns)` 定位 saver → 加载 checkpoint tuple 与 pending writes → 恢复 WorkflowState → 重新进入节点。节点可能重放，因此非确定性读取与外部副作用必须封装并幂等。

> **一致性边界**：Checkpoint 记录“执行到哪里”，Domain/Audit Event 记录“业务发生了什么”。二者通过 tenant/session/thread/run/job/correlation ID 关联，但互不伪装成对方。业务状态 + Business Event/Outbox 必须在 Business DB 单事务提交；Job/审批状态 + Platform Audit/Outbox 必须在 Platform DB 单事务提交。跨库只通过 ID 关联、幂等消费与对账投影最终一致；checkpoint 与外部系统同样以 idempotency key + execution ledger + reconciliation 处理至少一次执行。统一审计查询是可重建投影，不是第三个写入真相源。

### 3.3 Agent loop 语义、Workflow builder 与 MCP（ADR-006 / ADR-019）

#### 3.3.1 Agent loop 语义规格

`research_agent` 是唯一有界 model/tool 原语；V0.1 场景 A 和 V0.4 场景 B 只替换 Prompt、工具集合、预算和最终输出 schema，不复制循环。子图必须把 `model_turns/tool_calls_total/validation_repairs/seen_evidence_fingerprints/no_progress_streak/termination` 作为可 checkpoint 的私有 state，不能放在进程内变量中。以下规则必须逐项可断言：

1. **模型输出路由**：仅根据完整归一化后的 `ChatResult` 判边，不根据 provider 的 `finish_reason`、文本关键词或隐藏推理。`structured_output` 非空时，`tool_calls` 必须为空并进入 `validate`；`tool_calls` 非空时进入 `tools`，同时出现的可见文本只是 provisional content，不得作为最终答案；二者都为空时进入 `validate`。`finish_reason` 只作诊断。流式 tool args 未闭合、重复 tool-call ID，或同一响应混合 terminal structured output 与业务工具调用，均作为 Provider 契约错误失败，不执行工具。
2. **最终答案校验**：提供 `response_schema` 时，plain text 不能冒充结构化结果；必须取得 `structured_output` 并再次做本地 schema、商家/证据引用和数据分类校验。校验通过才到 `END`。Provider 归一化阶段抛出的 `StructuredOutputError`，以及 validate 阶段仅格式/schema 可修复的失败，允许追加一条由 Runtime 生成、只含错误码和字段路径的 system feedback，再回 `model` 一次；V0.1 固定 `max_validation_repairs=1`。该 repair turn 是 finalization-only：不暴露业务工具（synthetic 模式只暴露保留输出工具），必须直接返回结构化结果；再次返回业务 tool call、plain text 或非法结构即失败。不支持该能力、伪造实体/引用、权限失败和第二次失败直接结束，不再让模型“改写掩盖”。
3. **ToolResult → observation**：Tool Executor 先按 `result_schema` 校验、转成 Pydantic JSON mode、按 PolicyDecision 脱敏并生成模型可见投影；数组用 Oria schema 扩展 `x-oria-ordering=significant|set:<item_key>` 声明顺序语义，无序集合按稳定 item key 排序后再编码。每个 tool call 恰好追加一条 `Message(role="tool", tool_call_id=原 ID, content=<canonical JSON string>)`。JSON envelope 固定含 `observation_schema_version/tool_schema_version/ok/data/error/execution_id/trust_level/provenance/data_classification/object_ref`，使用 UTF-8、键排序和紧凑编码；不得包含异常栈、secret 或未授权字段。并行工具完成后仍按原 `tool_calls` 声明顺序追加 observation，不能按网络返回顺序漂移。
4. **大结果处理**：冻结配置 `max_inline_tool_bytes` 默认 32 KiB，按上述模型可见 JSON 的 UTF-8 字节数计算。超限时把完整的已校验/已授权结果写入 ObjectStore，observation 的 `data` 只保留有界 preview，`object_ref` 固定含 `key/media_type/sha256/byte_size`；引用仍受 tenant/ACL 和保留期约束，不能因转存绕过权限。ObjectStore 写入失败时该工具调用失败，禁止退回整段内联。
5. **工具失败策略**：执行并行 batch 前先对全部调用完成 name/allowlist、参数 schema、PolicyDecision 和预算预检；任一调用出现 `unknown_tool/invalid_arguments/permission_denied` 时整批不执行，并以 `policy_or_contract_violation` 终止。通过预检后，Provider 不处理工具重试；Tool Executor 只按 `ToolPolicy` 重试 `side_effect=false` 且 `ToolError.retryable=true` 的调用。重试耗尽或不可重试后，Agent loop 只消费最终 `ToolResult`：写工具的 `side_effect_unknown` 进入 `waiting/reconciliation`，其他写工具失败以 `side_effect_failed` 停止，Agent 不得自行重试或换工具掩盖；只读失败转成脱敏 observation，允许模型在剩余预算内改用其他 allowlist 工具或明确 abstain，但失败不算新证据。预检只是“是否允许开始”的全批门禁，不声称并行外部调用具备跨工具事务原子性。
6. **无进展谓词**：每个 tools super-step 对每条 `ok=true` observation 先构造稳定的 `semantic_observation={data, trust_level, provenance, data_classification, object_content_sha256}`；显式排除 `execution_id/provider_request_id/trace_id/timestamp/latency/retry_count` 等每次会变的元数据。再构造 `{tool_name, tool_schema_version, normalized_args, semantic_observation}`，按 §3.7 的 UTF-8/键排序/紧凑 JSON 规则编码后计算 SHA-256；ObjectStore 结果只使用 `object_ref.sha256`，不使用随机 key/签名 URL。若本 super-step 没有产生任何此前未见的 fingerprint（包括全部失败、空 tool-call batch、或相同调用得到相同语义观察），`no_progress_streak += 1`，否则把新 fingerprint 加入集合并清零。固定连续 **2** 个无进展 tools super-step 后以 `no_progress` 终止；“同一工具”本身不等于无进展，不同参数或真正变化的语义观察可算进展。
7. **预算与终止顺序**：进入 model 前检查剩余模型轮数/Token/成本，进入 tools 前检查 allowlist、工具调用总数和墙钟 deadline；一个并行 batch 会越过上限时整批不执行。Provider usage 记账后如已超限，不执行该响应建议的工具。强制终止不再额外调用模型，而是产生带 `reason/limits/observed_usage/last_safe_evidence_refs` 的 `AgentTermination`；预算、最大轮数/工具数、无进展和契约错误映射为 failed，副作用 `unknown` 映射为 waiting。业务上的“证据不足而 abstain”必须由一个通过 schema/evidence 校验的正常最终答案表达，不能与运行时强制终止混为一谈。

测试配置可以降低上限以缩短用例，但不得改变判定谓词、失败分类或“整批不执行”语义。所有预算和计数随 checkpoint 恢复，重复 resume 不能重置。

#### 3.3.2 Workflow 模式 builder

5 种模式 builder（`orchestrator/patterns.py`，V0.3 交付）：基于 LangGraph StateGraph 的便利构造器，非重造接线。返回编译后的 LangGraph 图。

```python
def routing(
    branches: dict[str, Node],
    classifier: Callable[[WorkflowState], str],
) -> Graph: ...
# 分类器选单分支执行

def parallelization(
    nodes: list[Node],
    join: Literal["concat", "merge"] = "concat",
) -> Graph: ...
# 扇出 N 节点并行，join 汇总

def orchestrator_workers(
    planner: Node,
    worker_factory: Callable[[Task], Node],
) -> Graph: ...
# Planner 动态分派 N worker（场景 B 归因用）

def evaluator_optimizer(
    generator: Node,
    evaluator: Callable[[WorkflowState], Score],
    threshold: float,
) -> Graph: ...
# 生成—评分—改写，达标出循环

def prompt_chaining(
    steps: list[Node],
    gates: list[Callable[[WorkflowState], bool]] | None = None,
) -> Graph: ...
# 线性链 + 可选闸门（场景 A 招商活动用）
```

场景 A 主要覆盖 prompt-chaining 与 parallelization；场景 B 主要覆盖 orchestrator-workers 与 evaluator-optimizer。

#### 3.3.3 MCP 传输与版本

**MCP 传输与版本**（ADR-019，`tools/mcp_client.py / mcp_server.py`，V0.7 交付）：

- 目标规范：**2026-07-28**；通过官方 MCP SDK 实现版本协商，并保留对已部署 2025-era server 的兼容测试。协议版本来自 SDK 常量/协商结果，不散落硬编码。
- Server（对外暴露 Oria 工具）：支持 `stdio`（CLI / 本地）与 2026 规范 HTTP endpoint；兼容 2025-era Streamable HTTP，不新增 legacy HTTP+SSE 实现。
- 2026 请求按无会话核心设计；服务状态通过显式 tool handle 传递。协议虽定义 Tasks extension，但当前实现必须先检查锁定版 Python SDK 的 capability/roadmap：SDK 未实现或未通过 conformance 时，Oria 以 `submit_job/get_job/cancel_job` 普通工具暴露显式 handle，并标记 Tasks extension “未实现”，禁止手写 framing 冒充支持。未来启用 Tasks 后仍与 Oria 内部 Job 建立显式映射，不混用 ID。
- Client（消费外部 MCP）：使用 SDK 能力发现/协商；外部 MCP 工具经适配注册到 `ctx.tools`，与内部 Tool 同 Protocol（§3.1）。远程连接必须校验 TLS、issuer/credential binding、scope 与 server allowlist。
- 分层：MCP = 跨进程外部协议；Oria 插件 = 同进程内部扩展。`mcp_client` 本身是个 Oria 插件，把外部 MCP 工具注册进 `ctx.tools`。

### 3.4 Prompt 版本管理（ADR-022，够用级）

不做 A/B 平台 / 灰度；只保证“改 Prompt 不改代码、可回滚”。采用 wheel 内 `src/oria/prompts/` package resources + `PromptManager`，不能依赖当前工作目录：

```text
src/oria/prompts/
├── merchant_selection/
│   ├── v1.jinja
│   └── v2.jinja             # 新增版本示例；运行期不自动选择
├── attribution_reasoning/
│   └── v1.jinja
└── _registry.py             # PromptManager
```

```python
class PromptManager:
    def render(self, name: str, *, version: int, **vars) -> str:
        # 运行期 version 必填；不得隐式选择文件名最大的模板
        # importlib.resources 读取；Jinja2 StrictUndefined，未定义变量报错（防漏字段）
        ...
```

约定：

- 版本号 = 文件名 `vN.jinja`（正整数，N 越大越新）；旧版保留可回滚。`list_versions(name)` 可供 CLI/开发工具发现版本，但不能参与运行期自动选择。
- 所有调用显式固定 version（如 `render("merchant_selection", version=2)`）；缺失、非正整数或不存在版本立即报错。改 Prompt = 新增 `vN+1.jinja` + 改代码引用，不原地改旧文件。
- 元数据：模板首行 `# meta: {desc, vars: [...]}`；CI 校验声明变量、未定义变量和每个固定版本的 golden render，避免运行时才发现模板缺字段。

### 3.5 IM 入站与通知抽象（可插拔：飞书 / 钉钉 / 大象）

入站与出站必须分开：`IngressAdapter` 负责验证飞书/钉钉 webhook 的签名、时间戳、防重放、消息去重和企业用户→可信 Principal 映射，再把消息规范化为 `InboundMessage/CampaignIntent`；CLI 由受信本地 profile 生成同一契约。模型只看到校验后的业务文本，不能从消息正文自报 tenant/role。V0.1 用 CLI + Mock ingress，V0.3 用 Mock 事件跑完整业务图，V0.6 才经 API 暴露真实 webhook endpoint；每个真实平台仍需独立 E 卡。

Agent 对外通知（发群 / 发人）通道化，不绑定单一 IM。通用基础工具 `send_im` 由配置 / 注册表路由；场景 A 的领域工具 `send_merchant_notification` 先按商家与结果版本渲染内容、执行权限/敏感字段检查和业务去重，再委托 `send_im`。三平台对齐为统一 `target`（群 ID 或用户 ID）+ `text`。命中与 Provider / Tool 同 seam 思想（ADR-016 / 019）。

三内置实现（`tools/builtin/im/`，各含 `_mock/`）：

| 通道 | 实现 | 真实接入要点 |
| --- | --- | --- |
| 大象（Daxiang） | `DaxiangChannel` | 美团内部 IM，bot / webhook |
| 飞书（Feishu / Lark） | `FeishuIngress + FeishuChannel` | 入站事件验签/挑战/去重/用户映射；出站 bot + openid/chatid |
| 钉钉（DingTalk） | `DingTalkIngress + DingTalkChannel` | 入站回调验签/时间戳/去重/用户映射；出站机器人签名 |

底层通用工具 `send_im`（注册到 `ctx.tools`；通用场景可直接调用，场景 A 经 `send_merchant_notification` 委托，不直接调某平台）：

```python
async def send_im(
    target: str,
    text: str,
    ctx: Context,
    channel: str | None = None,
    idempotency_key: str | None = None,
) -> ToolResult: ...
```

`channel=None` → config 默认通道；`ctx.notifier` 路由到对应 IMChannel。

> **OSS 可跑**：三通道均有 `_mock`，CI 免 key；真实接入层隔离，V0.8 演示按已完成 Live 验证的通道切换。配置见 §4.3 `im:` 段。

### 3.6 存储扩展 seam（社区版 / 正式版切换点）

> 缓存与对象存储也走 Protocol，社区版内置轻量实现，正式版插拔。向量 / 平台 DB / 招商 DB seam 见 §3.1 / §3.2 / §5.1。

```python
class CacheStore(Protocol):
    async def get(self, key: str, ctx: Context) -> bytes | None: ...
    async def set(
        self,
        key: str,
        val: bytes,
        ctx: Context,
        *,
        ttl: int | None = None,
    ) -> None: ...
    async def delete(self, key: str, ctx: Context) -> None: ...

# 社区版：内存 dict；正式版：Redis


class ObjectStore(Protocol):
    async def put(self, key: str, path: str, ctx: Context) -> str: ...
    # 返回可访问 url / key

    async def get(self, key: str, dest: str, ctx: Context) -> str: ...
    # 下载到 dest

# 社区版：本地目录；正式版：MinIO / S3
```

切换由 §4.3 `edition + storage` 决定；两实现同 Protocol，业务无感。本地实现必须把 key 规范化到配置根目录内并拒绝 `..`/绝对路径；远端实现使用短期签名 URL，不把永久凭证返回给 Agent。

语义缓存只允许保存只读、可重新计算且通过数据分类策略的响应；key 至少绑定 tenant、subject/policy、provider/model、prompt、tool schema 与 knowledge version。审批决定、写工具结果、敏感响应、Memory 正文和权威业务事实禁止进入语义缓存；缓存不可用时只能降级为重算，不能改变授权或业务真相。

### 3.7 HITL、工具副作用与幂等（ADR-010 / ADR-024）

所有工具先按 `ToolPolicy` 分类。只读工具可按策略重试；所有写工具都必须是独立、可幂等、受 PolicyEngine 约束的副作用节点，但不是所有外部写都需要固定 HITL。只有 `approval_mode=required`，或 `approval_mode=conditional` 经当次策略判定需要审批的工具，才采用“审批节点 → 独立副作用节点”；不得在必需的 `interrupt()` 之前执行副作用。`business_confirmation=true` 表示还要满足领域确认链，与平台 HITL 是两套独立机制。

| 工具类型 / 代表工具 | 风险 | `approval_mode` | 执行约束 |
| --- | --- | --- | --- |
| 规则/商家/商品查询 | 低 | `none` | 只读、强制 ACL pre-filter，可按策略重试 |
| `persist_campaign_draft` | 中 | `none` | 只写本地草案，重新鉴权，无外部投放 |
| `materialize_coupon_batch` / `publish_recruitment` | 高 | `required` | 共用一次 `launch_approval`，但分开记账和执行 |
| `upsert_enrollment_items` / `link_coupon_batch` | 中 | `none` | 幂等领域写，必须满足已冻结的确认链与规则不变量 |
| `submit_assortment` | 中 | `conditional` | 默认在确认链完成后自动提交；Adapter 声明不可撤销、广范围或策略升级时才 HITL |
| `publish_consumer_placement` | 高 | `required` | 需 `consumer_publish_approval`，只能投放已入选且券关联有效的版本 |
| `send_merchant_notification` | 中 | `conditional` | 默认自动；含敏感内容、大范围或非标准模板时升级 HITL |

**审批绑定**：`approval_id` 必须绑定 `tenant_id + tool_name + canonical_args_hash + checkpoint_id + policy_version + expires_at`。恢复时重新鉴权并校验上述字段；参数、checkpoint 或策略变化即令旧审批失效。审批状态、申请人、审批人、决定、时间和原因与 Platform `audit_events/outbox` 同事务写入；执行批准后的业务副作用时，再在 Business DB 写对应 Domain/Audit Event，不跨库包事务。`LaunchPlan` 以受控复合 command 名占用 `tool_name`，其 `canonical_args_hash` 覆盖所有声明子步骤；不能用自由形 action 绕过 ToolPolicy。

`canonical_args_hash` 不对模型输出的原始 JSON 直接哈希：先经 Tool Pydantic schema 校验并转为 JSON mode，金额/比率用 Decimal 规范字符串、时间统一 UTC ISO-8601、集合按 schema 约定排序，再对 `{tool_name, tool_schema_version, normalized_args}` 使用 UTF-8、键排序、无多余空白的 JSON 执行 SHA-256。未知字段、NaN/Infinity、无时区时间和不可规范化值在审批前拒绝；日志/审计保存 hash 和脱敏摘要，不保存敏感原参数。

**执行账本 `tool_executions`**：

- 唯一键：`(tenant_id, tool_name, idempotency_key)`；idempotency key 由稳定业务标识和参数哈希生成，不使用随机重试次数。
- 状态：`reserved → executing → succeeded | failed | unknown`；调用前原子 reserve，成功结果先落账再恢复图。
- 重试先读取历史结果；`unknown` 进入对账/补偿流程，不盲目重复投放。
- 高风险或不可逆工具只允许 `fail_stop`；`on_failure=warn` 仅可用于显式标记为 non-critical 的只读/通知节点。

`materialize_coupon_batch`、`publish_recruitment`、`submit_assortment`、`publish_consumer_placement`、`send_merchant_notification` 均实现幂等键。场景 A 固定有两个平台高风险 HITL 闸门：`launch_approval` 绑定预定义复合命令 `LaunchPlan`（规则快照、活动草案、券批次草案、招商投放范围、素材版本、补偿策略版本与两个子步骤参数哈希），批准后才按可恢复 saga 依次物化券批次并投放商家侧；`consumer_publish_approval` 绑定招后选品结果、券关联结果与 C 端投放参数，批准后才执行 C 端投放。复合审批只允许覆盖 schema 中预先声明的子步骤，任一子步骤参数变化都会使整体审批失效；不得把它当成可复用的通用授权。两次审批互不复用。

`LaunchSagaState` 固定为 `planned → coupon_materialized → recruitment_published → completed`，失败只能转入 `compensation_pending / reconciliation_required / failed`。券批次已物化而招商投放失败时，不得把本地状态回滚成“券未创建”；只有 Adapter 已声明且通过契约验证的可幂等补偿命令才能执行补偿，否则 fail-stop 并进入人工对账。每个子步骤的 reservation、receipt、args hash 和补偿结果单独记账，整体 `plan_hash` 只用于绑定审批，不取代子步骤幂等键。

商家确认、销售确认、销售经理确认和超时处理属于 `BusinessConfirmationPolicy` 驱动的业务确认链，不等同于上述 Tool/HITL 安全审批。其步骤按冻结的 `CampaignRuleSnapshot` 动态实例化，可为零到多级；超时默认升级或拒绝，只有规则明确授权且 PolicyEngine 允许时才可自动确认，禁止把缺省值解释为自动通过。`upsert_enrollment_items`、商品自动圈选、报名商品与券批次关联均为中风险幂等业务写：每次写入前重新鉴权并满足确认链/规则不变量，但不额外制造固定 HITL。

### 3.8 Durable Job、外部事件等待与 API 契约（ADR-014 / ADR-025 / ADR-029）

Job 是数据库持久化状态机，不使用 FastAPI 进程内后台任务承载跨天执行：

```text
queued → running → waiting_approval ─┐
              └─→ waiting_event ────┼→ queued
              └─→ retry_scheduled ──┘
queued/running/waiting_approval/waiting_event → cancelled
waiting_approval --reject/expire--> failed
waiting_event --expire--> queued | failed | cancelled（按已冻结 timeout_action）
running → succeeded | failed | cancellation_requested → cancelled
```

`jobs` 至少包含 `job_id/tenant_id/session_id/thread_id/run_id/requester_subject_id/status/attempt/max_attempts/lease_owner/lease_epoch/lease_expires_at/heartbeat_at/retry_at/timeout_at/cancel_requested/schedule_policy/idempotency_key/webhook_id/accepted_checkpoint_ns/accepted_checkpoint_id/created_at/updated_at`。`external_waits` 保存事件类型、资源/期望版本、checkpoint、过期与 timeout action；`integration_event_inbox` 以 `(tenant_id, adapter_id, source_event_id)` 去重并保存标准化 `IntegrationEventEnvelope`、验签主体、payload hash、处理状态与 wait 关联。Adapter 必须先从受限 `InboundRequest` 验签、防重放、做 tenant/resource 映射，再生成 envelope；请求不得自带 checkpoint/wait ID 或自报 actor 权限。原始 body 只在进程内用于验签，不进日志、checkpoint 或 inbox；inbox 只持久化脱敏 payload 和 hash。事件先持久化 inbox，再以 CAS 解析一个匹配 wait 并把 Job 置回 queued；重复、乱序、错误版本、未授权或事件类型不匹配均不得恢复 Graph。

场景 A 的最小事件 union 固定为 `merchant.enrollment_upserted / enrollment.window_closed / selection.decision_recorded / selection.completed`。报名分支只在已冻结的 `enrollment_window` 内接受 upsert；窗口关闭事件或时钟超时解析 wait 后才可 join。关闭后到达的报名事件默认记录为 `late_rejected`，不修改已接受版本；若业务规则明确支持补报，必须创建新 enrollment version 并使下游审批失效。`merchant` 模式等待窗口关闭，`auto` 模式运行确定性圈品后可直接结束报名分支，`hybrid` 模式同时等待自主报名窗口和自动圈品完成，再按业务唯一键去重汇聚。

只持久化 requester 稳定引用和提交快照 hash，不保存用户 bearer token；worker 恢复时从可信主体目录重新加载 actor 当前属性，并以自己的 service identity 作为 executor 重新鉴权。Worker 通过数据库行锁/compare-and-set 领取 lease，每次 claim 原子递增 `lease_epoch` 并周期 heartbeat；lease 过期可被其他 worker 恢复。执行上下文携带 fencing token，领域提交与副作用 reservation 前均复核 `(lease_owner, lease_epoch)`；失去 lease 的旧 worker 即使复活也不得继续提交。状态转换必须 compare-and-set 并记录 `job_events`。

每个 lease epoch 使用独立 `checkpoint_ns`。Worker 从 job 指向的 accepted checkpoint 创建新 attempt；只有持有当前 fencing token 的 worker 才能以 CAS 推进 `accepted_checkpoint_ns/id`。旧 worker 即使晚写入官方 saver，也只产生不可达的 orphan checkpoint，不能覆盖下一次 resume 游标；orphan 由保留策略清理。这样不要求篡改官方 saver 的内部并发语义。

标识关系：`tenant` 拥有多个 `session`；一个 session 可包含多个独立 `thread`；一次提交生成一个 `job` 和一次或多次 `run/attempt`，但始终恢复同一 thread；每个 checkpoint 属于 thread/checkpoint namespace。所有外部 ID 使用不可猜 UUID/ULID，且不可替代 tenant policy 校验。

API v1 最小契约：

- `POST /v1/sessions`、`POST /v1/jobs`（要求 `Idempotency-Key`）
- `POST /v1/ingress/feishu/events`、`POST /v1/ingress/dingtalk/events`（平台 challenge/验签、防重放、source message 去重；只由 IngressAdapter 生成 Principal/Job）
- `POST /v1/integrations/{adapter_id}/events`（报名/选品等企业事件；adapter 验签、事件 ID 去重、resource/version/wait 绑定，不接受客户端直接指定 checkpoint）
- `GET /v1/jobs/{job_id}`、`POST /v1/jobs/{job_id}/cancel`
- `GET /v1/jobs/{job_id}/events`（SSE，支持 `Last-Event-ID` 断线续传）
- `GET /v1/approvals`、`POST /v1/approvals/{approval_id}/decision`
- 错误统一为 `{code, message, request_id, retryable, details}`；所有资源访问同时校验 tenant 与 action/resource policy。

V0.6 导出并版本化 OpenAPI v1 snapshot，对 endpoint、必填字段、枚举、统一错误和 SSE union 执行 breaking-change diff；发现破坏性变更必须升 API 版本或提供兼容期，不得只重新生成 Web client 掩盖破坏。V0.8 Web client 只从该 snapshot 生成，生成代码与 snapshot 必须在同一变更中提交。

开发模式本地身份只能在显式 profile 下启用并绑定回环地址；联网/生产 profile 必须校验 JWT/OIDC 的签名、issuer、audience、expiry，并由服务端映射 `Principal`。客户端传入的 tenant/roles/header 只可作为请求数据，不能成为授权事实；跨租户资源统一按策略拒绝，避免通过状态码或错误详情泄漏资源是否存在。

V0.1 `community+demo` 由受信本地 profile 构造固定 `local-community` tenant 和 `local-operator` subject，用于给 catalog、Merchant、Checkpoint metadata 填充稳定租户边界；它不从 CLI 自由 header/参数接受 roles，也不是生产认证。V0.2 建立显式身份/策略表后，仍保留该值作为可迁移的社区种子，不为 MVP 重写 tenant ID。

V0.8 Web 首版固定为 OIDC Authorization Code + PKCE 公共客户端：校验 state/nonce/issuer/audience，access token 只保存在页面内存，不申请或持久化 refresh token，页面重载后重新认证；禁止把 bearer/refresh token 写入 localStorage/sessionStorage/IndexedDB。CORS 只允许显式 origin，不使用 `*`。同源网关/BFF 保留为正式部署 adapter，但不与首版同时实现；启用前另写 ADR，其 cookie session 必须启用 HttpOnly/Secure/SameSite、CSRF token 和严格 Origin 校验，且 token 只留服务端。社区开发身份不进入生产构建默认配置。

SSE 事件使用带版本的 union：`job.status / graph.update / model.delta / tool.status / approval.required / external.waiting / external.event_accepted / error / done`，包含 `event_id/sequence/run_id/timestamp`，不得输出隐式思维链、凭证或未脱敏工具参数。Webhook 使用 HMAC 签名、时间戳和 delivery ID，具备指数退避、投递日志、重放防护和死信状态。

取消采用协作式语义：只能阻止尚未提交的后续步骤，不能撤销已提交的数据库事务或已被外部系统接受的请求；API 必须返回当前状态，必要时进入补偿/对账，而不是把 `cancelled` 等同于“副作用从未发生”。

### 3.9 企业安全、审计、可观测与 Eval 基线（ADR-010 / ADR-015 / ADR-020）

**安全边界**：认证支持开发模式本地身份与正式版 OIDC/SSO；用户、worker、插件和 MCP server 均使用独立 service identity 与最小权限凭证。PolicyEngine 在 API 资源访问、Retriever、Memory、工具路由、工具执行和审批恢复处统一决策，默认拒绝。Secret 不进入 prompt/state/event/trace；工具网络访问使用 egress allowlist，阻断 loopback/link-local/metadata IP、任意 URL、路径穿越和未授权 shell。Prompt injection 检测只是纵深防御，不作为授权依据；不可信 RAG/Tool/MCP 内容不能提升指令优先级或扩大工具集合。

威胁模型不是 V0.8 才补的文档：V0.1 先画数据流/信任边界，V0.2 加 RAG/数据污染，V0.3 加审批与副作用，V0.6 加 API/身份/多 worker，V0.7 加 MCP/插件；V0.8 对累积模型做攻击演练和残余风险复核。

**审计**：Platform/Business `audit_events` 使用同一 EventEnvelope，记录 actor/action/resource/decision/policy_version/args_hash/result/correlation；payload 按字段脱敏，禁止记录密钥、完整 prompt、隐式思维链和无必要 PII。正式版采用 append-only 权限、数据库审计或 hash chain/外部 WORM 导出增强防篡改，并定义保留、查询授权、导出和删除例外。对无源状态事务的读取/拒绝决策，经 Platform AuditService 单独 append；restricted 级别在 production 下审计落库失败必须 fail closed，不得只打日志后放行。

**可观测**：从 V0.1 起输出结构化 Console JSON、correlation/run ID、Provider/Retriever/Tool 延迟与 usage；V0.3 加入审批、execution ledger 与 audit 关联；V0.6 跨 API/worker/webhook 传播 W3C trace context；V0.8 再接真实 Collector/后端并完成端到端 Trace。最终一条 trace 覆盖 API → Job → Graph → Node → Provider/Retriever/Tool → HITL → Webhook。Span 使用稳定字段：`tenant_id`（受控基数）、`session_id/thread_id/run_id/job_id`、provider/model、tool、latency、token、cost、cache hit、attempt、retryable/error type；tenant/session 等高基数标识不作为 metrics label。Logs/Traces/Metrics 分离 exporter，内容采集默认关闭。这样每阶段只增强 exporter 和关联范围，不在 V0.8 重写埋点模型。

**Eval 分层**：

- PR gate：类型/契约、确定性 workflow、tool 参数、ACL/跨租户、恢复、幂等和安全回归。
- Golden regression：版本化数据集、prompt/model/tool schema，保存每次 run 与差异。
- Offline/nightly：冻结 judge model/prompt/rubric/temperature，重复采样并报告方差；人工抽检校准 judge。
- 指标分离：质量、groundedness/citation、工具正确率、安全、成本、端到端时延；没有真实 run 证据前不写预设提升数字。

CI 落地以详细路线 §2.3 为唯一执行口径：PR 的 `eval-golden` 是禁外网的独立 required job，只使用 MockLLM 录放/Fixture 和版本化 baseline，确定性指标不允许负回归；真实 Provider 只进入带显式 target 与硬成本预算的 nightly/manual workflow，并作为 release/能力声明证据，不读取 fork PR secrets，也不拿随机 Live 结果替换确定性门禁。dataset/baseline/gate/budget 配置统一放在根 `eval/`，harness 实现在 `src/oria/eval/`。

---

## 四、LLM Provider 抽象（核心设计）

接口：

- `async LLMProvider.chat(messages, ctx, tools=None, options=None) -> ChatResult`。
- `LLMProvider.chat_stream(messages, ctx, tools=None, options=None) -> AsyncIterator[StreamEvent]`：类型化流事件供 Agent loop 与 SSE 消费。
- `capabilities(ctx) -> ProviderCapabilities`：声明 tool calling、structured output、reasoning、并行工具、多模态和 token 上限；Runtime 在请求前校验，不靠失败后猜测。

Provider 统一实现 connect/read/total timeout、取消传播、限流信息和标准错误：`AuthenticationError / RateLimitError / ContextLengthError / InvalidRequestError / ProviderUnavailable / TimeoutError`。每个错误携带 `retryable/retry_after/provider_request_id`；仅对 retryable 错误退避重试，禁止在 Provider 内重试有副作用工具。

两个实现：

**1. OpenAICompatProvider（覆盖 DeepSeek / Kimi / 智谱 / OpenAI）**

配置驱动覆盖 4 家，但“OpenAI compatible”不等于能力完全一致。每个 profile 还必须固定 `api_dialect=chat_completions|responses`，adapter 据此选择 endpoint、请求/流事件和结构化输出映射，禁止把两种 payload 混发。下面模型仅作经验证的示例 profile；运行时模型目录、上下文窗口与能力来自配置或厂商模型查询接口，静态表不得作为唯一真相源：

| 厂商 | Base URL | 模型 |
| --- | --- | --- |
| DeepSeek | `https://api.deepseek.com` | `deepseek-v4-flash` / `deepseek-v4-pro`（2026-08 核验） |
| Kimi | `https://api.moonshot.cn/v1` | 由 `${MOONSHOT_MODEL}` 或官方 model-list 选择 |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | 由 `${ZHIPU_MODEL}` 或官方模型目录选择 |
| OpenAI | `https://api.openai.com/v1` | 由 `${OPENAI_MODEL}` 或官方 Models API 选择 |

归一化点（面试 hook）：各家 OpenAI 兼容但非真统一——

- 推理模型可能返回 `reasoning_content` 或专用 reasoning block，必须与可见 `content` 分离；具体字段由 adapter capability/contract test 验证。
- 智谱有 `web_search` 内置工具，tool-call 字段格式略异。
- Kimi/智谱等模型的上下文、输出和工具能力会随模型 ID 变化，必须读取 capability 并在请求前校验，不能沿用厂商级固定上限。
- Provider 层统一公共语义，同时通过 `ProviderCapabilities` 和 `raw_response/raw_type` 保留不能无损归一化的信息，禁止静默丢字段。

`raw_response/raw_payload` 只用于有界、受控调试，默认不持久化、不进入 API/SSE/Eval 报告；启用时必须限长、字段脱敏并执行短保留期，reasoning/隐式思维链仍不得保存。Tool/MCP 返回值同样先做 schema、大小、分类和 trust/provenance 校验，超大内容转 ObjectStore 引用而不是整段塞入模型上下文。

**2. AnthropicProvider（Claude）**

- API：`https://api.anthropic.com/v1/messages`
- 模型：由显式 profile 选择，并在 Live 验证时记录不可变 model ID/版本；静态架构文档不承诺滚动别名长期可用。
- Schema 不同：`system` 是独立参数，消息内容是 content block 数组；工具是 `tool_use / tool_result` block（不是 OpenAI 的 `tool_calls / tool role`）。Provider 层做双向转换。

**用户选择**：`~/.oria/config.yaml`、环境变量或 CLI 选择 `edition/runtime_profile/llm_profile`。零配置默认 `community+demo+mock`；真实体验显式切 `community+standard+DeepSeek`。

> **ADR-001 已接受**：当前采用 Oria 薄 Provider adapter 与显式能力矩阵，决策、代价与迁移路径见 [`docs/adr/ADR-001-provider-normalization.md`](docs/adr/ADR-001-provider-normalization.md)。

### 4.1 结构化输出归一化

`ChatOptions.response_schema` 接受 Oria 规范形 `ResponseSchema{name, json_schema, strict}`，不是任一厂商的原始 payload。Runtime 在发请求前完成 schema 自检（合法 JSON Schema、稳定唯一 name、禁止与真实工具或保留名冲突），Provider adapter 再按**模型 profile**而非厂商名称选择下列显式策略：

| 策略 | 适用条件 | Provider 边界行为 | 内部结果 |
| --- | --- | --- | --- |
| `native_json_schema` | 该 profile 的真实契约测试确认原生 JSON Schema | adapter 按 dialect 映射为该 API 实际要求的 `response_format`、`text.format` 或 `output_config.format`；不得假设同厂商所有模型一致 | 解析并本地校验后写 `ChatResult.structured_output` |
| `synthetic_tool` | 无原生 schema，但 profile 可靠支持 tool calling | 注入保留工具 `__oria_submit_response__`，其 input schema 等于 `ResponseSchema.json_schema`；Anthropic 等 adapter 可映射为普通 `tool_use` | adapter 截获该保留调用并写 `structured_output`，绝不交给 ToolRegistry/ToolExecutor |
| unsupported | 两者均未通过 capability/契约测试 | 请求前抛 `UnsupportedCapabilityError`，不发网络请求 | 无静默降级 |

保留工具可以与业务工具一同暴露，让模型先调用业务工具、最终再提交结构化结果；同一响应若同时包含保留工具与任何业务工具，归一化必须失败，不能猜执行顺序。合成工具名不得由插件覆盖，也不进入工具 allowlist、审计账本或副作用计数，但其提交占一次 model turn。`ProviderCapabilities.structured_output_modes` 报告实际可用策略，`structured_output=true` 仅是派生便利字段；能力未知即按 unsupported 处理，不采用“提示模型输出 JSON 后尽力解析”的隐式 fallback。

Anthropic 不再写死为 tool-based fallback：当前支持 structured outputs 的 Claude profile 优先把 native 策略映射到 `output_config.format`，并可与 strict tool use 同请求使用；旧模型或 capability 探针不支持 native 时，才选择 synthetic tool。OpenAI-compatible 各 profile 同样必须以实际模型 capability/CT 决定映射，不能因 API 路径兼容就假定 DeepSeek/Kimi/智谱与 OpenAI 的 JSON Schema 子集完全相同。当前 DeepSeek `/chat/completions` 的 `response_format` 只声明 `json_object`，而 `/responses` 的 `text.format` 才声明 `json_schema`；因此 V0.1 DeepSeek profile 固定 `api_dialect=responses` 来满足严格 `ResponseSchema`，不得向 Chat Completions 发送 OpenAI 风格的 `json_schema` payload。

无论采用哪种策略，adapter 都必须把结果先解码为 JSON，再用同一 `ResponseSchema` 做本地严格校验；`strict=true` 时未知字段同样拒绝。非法 JSON、schema 不匹配、多个保留提交、保留调用与业务调用混合均映射为 `StructuredOutputError`，由 §3.3.1 的一次受限 repair 规则处理。原始 JSON 文本不能直接进入领域写入；只有 `structured_output` 的已校验对象可交给 validate/领域 Service。Provider CT 必须对 native、synthetic、unsupported、非法结果和混合调用运行同一套断言。

### 4.2 Anthropic 双向转换（实现方据此写 `providers/anthropic.py`）

内部规范形 = OpenAI 式（§3.1 值类型）；AnthropicProvider 在边界做双向转换，上层无感。

**内部 → Anthropic（请求）**：

| 内部 | Anthropic |
| --- | --- |
| `Message(role="system")` | `system` 参数（独立）；多条 system 拼接为一段 |
| `Message(role="user", content=str)` | `{role: "user", content: [{type: "text", text}]}` |
| `Message(role="assistant")` 含 `tool_calls` | `{role: "assistant", content: [{type: "text"}, {type: "tool_use", id, name, input}]}` |
| `Message(role="tool", tool_call_id, content)` | `{role: "user", content: [{type: "tool_result", tool_use_id, content}]}`（tool_result 进 user 消息） |
| `ToolSpec` 列表 | `tools` 参数 `[{name, description, input_schema: json_schema}]` |

**Anthropic → 内部（响应 → ChatResult）**：

- 响应 content blocks 保持有序：`{type: "text"}` → `TextBlock`；普通 `{type: "tool_use", id, name, input}` → `ToolCallBlock + ToolCall`；保留名 `__oria_submit_response__` 按 §4.1 截获并校验为 `structured_output`，不得生成可执行 ToolCall。便捷纯文本由 `ChatResult.text` 派生，不把原 blocks 不可逆拼接成字符串。
- usage 映射到统一 `input_tokens/output_tokens/cache_*/reasoning_tokens`；无法提供的字段为 `None`，不能填 0 冒充真实值。
- 保留 `finish_reason/request_id/refusal/raw_response`，日志默认不记录 raw payload。

**流式（stream events → StreamEvent）**：

- `content_block_delta(text_delta)` → `TextDelta`。
- `content_block_delta(input_json_delta)` → `ToolCallDelta`；只在完整 JSON 通过 schema 校验后执行工具。
- reasoning → `ReasoningDelta`，只供受控调试/模型续写，不经 API/SSE 默认外发，也不写普通日志。
- token usage → `UsageDelta`；结束和异常分别为 `Done/ProviderError`。每个事件有单调 sequence，消费者可重组并检测缺片。

### 4.3 配置 Schema

```yaml
environment: development  # development | test | production
edition: community        # community | production
runtime_profile: demo     # demo | standard；production 只允许 standard

llm:
  active_profile: mock    # CLI: --llm-profile deepseek
  profiles:
    mock:
      provider: mock
      api_dialect: mock
      model: mock-demo
    deepseek:
      provider: deepseek
      api_dialect: responses  # strict ResponseSchema 使用 /responses 的 text.format
      model: deepseek-v4-flash  # 2026-08 核验；升级前先跑 capability/contract suite
      api_key: ${DEEPSEEK_API_KEY}
      base_url: https://api.deepseek.com
    kimi:
      provider: kimi
      api_dialect: chat_completions
      model: ${MOONSHOT_MODEL}  # 必填；以调用时官方 model list 为准，避免固化退役别名
      api_key: ${MOONSHOT_API_KEY}

embedding:
  active_profile: fixture
  profiles:
    fixture:
      provider: fixture   # 仅 demo/test
    bge:
      provider: sentence_transformers
      model: BAAI/bge-small-zh-v1.5
      revision: ${ORIA_BGE_REVISION}  # 实现时锁定 commit/revision；trust_remote_code=false

# IM 通道（见 §3.5）
im:
  default: mock  # mock | daxiang | feishu | dingtalk
  channels:
    daxiang:
      webhook: ${DX_WEBHOOK}
    feishu:
      app_id: ${FS_APP_ID}
      app_secret: ${FS_APP_SECRET}
    dingtalk:
      webhook: ${DD_WEBHOOK}
      secret: ${DD_SECRET}

log_level: INFO
data_dir: .oria-data   # community 默认；CLI/env 可覆盖，解析后必须是绝对路径
storage:
  vector: chroma       # community: chroma | production: milvus
  platform_db: sqlite  # community: sqlite | supabase | production: postgres
  biz_db: sqlite       # demo/community 默认 sqlite；supabase 可选；production: dms | mysql
  cache: memory        # community: memory | production: redis
  object: local        # community: local | production: s3 | minio

telemetry:             # logs / traces / metrics 分开配置，eval 不属于 telemetry exporter
  log_exporter: console_json   # console_json | file_json | enterprise
  trace_exporter: console      # console | otlp | langfuse
  metric_exporter: console     # console | otlp
  capture_content: false       # 默认不采集 prompt、completion、tool 参数原文
```

加载优先级（高 → 低）：CLI `--runtime-profile / --llm-profile` > 环境变量 `ORIA_RUNTIME_PROFILE / ORIA_LLM_PROFILE` 与对应 profile secret/model > `~/.oria/config.yaml` > `community+demo` 零配置默认。只解析并严格校验 active profile 的变量；active profile 中未解析的 `${...}` 必须启动失败，inactive profile 不因缺 Key 阻塞。禁止同时使用顶层 provider/model 与 active profile 两套来源。

**无 key 策略**：`community+demo`、`environment=test` 或显式 `llm.active_profile=mock` 允许 Mock；`community+standard` 选择真实 profile 后缺 key 必须启动失败；`production` 禁止 Mock/FixtureEmbedder 并 fail closed。config 用 `pydantic-settings` 解析，禁止 secret 进入 repr/log；生产 secret 来自环境或 secrets manager。

Factory 在启动时把多来源配置解析为只读 `ResolvedRuntimeConfig`，校验组合矩阵并生成不含 secret 的 `config_fingerprint`。Graph、Job 和验证报告只记录 fingerprint/active profile，不再读取原始环境变量，避免运行中配置漂移。

**装配与生命周期顺序**：CLI 每次进程、API/worker lifespan 只调用一个 `build_runtime()` async factory：① 解析/冻结 config；② 创建 DB engine/session factory、Repository、Saver、Vector/Cache/ObjectStore；③ 把 Repository 注入领域 Service，再创建 Provider/Embedder/Retriever/Policy/Notifier；④ 注册 Tool/Guardrail/Node/Agent；⑤ 编译 Graph；⑥ 返回 RuntimeServices，由其 `AsyncExitStack` 在进程退出时按逆序关闭插件、HTTP client、模型资源和连接池。每个请求/Job attempt 在重新认证后调用 `runtime.new_context(actor, executor, ids)` 生成独立 Context，不得把 actor、tenant、session 或 run ID 写回 RuntimeServices/全局变量。Repository 不进入 prompt、Graph state 或 Tool schema，Tool 只能经 `ctx.domain` 调用领域 Service。模块内部禁止自行读取环境变量、创建全局 client/engine 或在 import 时连接外部服务。Factory 任一步失败必须完整 unwind，不能留下半初始化 Registry；Context 并发契约测试必须证明两个 tenant/run 共享连接池但不共享任何执行 metadata。

`AsyncExitStack` 只属于 `build_runtime()` 与启动期受信插件 `setup`，RuntimeServices 对外可用前即封存注册阶段；Graph node、Tool、request/job Context 和运行期中间件不得访问 `_exit_stack` 或追加进程级 teardown。单次调用需要的临时资源必须在节点内部用局部 `async with` 并在节点返回前关闭，不能把关闭动作寄存在 Context 或 checkpoint 外状态。V0.7 插件安装/卸载仍通过受控重启完成，不以运行中修改 exit stack 冒充热插拔。这样 checkpoint resume 只依赖可重建的 RuntimeServices 与已持久化 state，不依赖上一次进程中增长过的清理栈。

编译后的 Graph 只捕获 RuntimeServices 中的无主体资源，每次 `invoke/stream` 通过安装版 LangGraph 的 `context_schema/Runtime` 入口传入 Context；节点不得从 closure、module global 或上一次 invocation 读 actor/run metadata。若锁定版本的符号名不同，只在 `orchestrator/runtime.py` 写薄 adapter，不改变这一隔离契约。

社区运行数据统一落到 `data_dir` 下的 `sqlite/platform.db`（身份/知识目录/会话/Job/审批/平台审计）、`sqlite/business.db`（Merchant/招商领域/领域事件/业务审计/Outbox）、`chroma/`、`objects/` 和 `reports-tmp/`。包内维护 platform/business 两条独立 Alembic revision 链和 version table；`oria db upgrade --target platform|business|all` 统一驱动。CLI 以程序化 Alembic Config 从 package resources 定位 revision，不依赖源码根目录的 ini；根目录两个 ini 只供开发调试。`oria data init` 从 V0.1 起复用同一 migration runner，完成两库升级、demo seed 和 saver setup，不再实现第二套建表逻辑。官方 saver 自管 checkpoint 表，Alembic 只管 Oria 表，两者不得互相 `create_all`。默认 `.oria-data/` 必须加入 `.gitignore`。测试必须显式注入 pytest 临时目录，禁止读写开发者 Home 或复用 demo 数据。production profile 必须显式配置持久化路径/远端后端，不接受相对 `data_dir`。

---

## 五、Hero 场景（双场景：Workflow + Agent）

Oria 同时支持两类真实业务场景，对应架构 Workflow（DAG）与 Agent（loop）两条主线。面试双场景一起讲，直接命中 ADR-006（Workflow vs Agent）。

**场景 A：招商活动自动化（Workflow / DAG，步骤预定）**

运营在飞书、钉钉或 Agent CLI 发起：“开始暑期大促华东区招商，按现行规则圈选商家与商品、配置优惠，完成招后选品和 C 端投放，并把结果通知商家”。大象作为企业可选 IM Adapter 保留，但不属于本场景必需入口。

完整业务流程固定为以下 10 步；实现不得把“招商投放”和“C 端投放”合并成一个含糊的 `dispatch`：

1. **需求受理与鉴权**：接入层把飞书/钉钉消息或 CLI 输入规范化为 `CampaignIntent`；校验发起招商、读取规则/商家/商品、使用销售组织和发送通知的权限。IM webhook 还需验签、防重放并映射可信 actor，不能采信消息中的自报角色。
2. **RAG 生成规则快照**：`search_campaign_rules` 读取招商 SOP、活动要求和规则，将来源版本与逐字段引用固化为 `CampaignRuleSnapshot`：
   - 基础信息：活动模板、活动商品范围、活动时间、报名时间、活动类型。
   - 招商范围：行业类目、门店城市、客户黑白名单、支持的报名系统、可报名销售组织。
   - 报名规则：客户圈选规则（含关联活动报名）与团单/商品圈选规则（价格、类目、关键词）。
   - 招后选品规则：选品策略引用/版本、选品执行模式与完成条件；与报名阶段的自动圈品规则分离。
   - 优惠档位：基础档、膨胀档；固定金额、阶梯出资或折扣率，以及币种、舍入、预算上限。
   - 确认规则：商家、销售、销售经理确认顺序与超时动作。
   - 商家端素材：活动标题、头图、介绍、标签。
   缺字段、来源冲突、时间窗非法或引用失效时进入补充/人工澄清，不允许 LLM 猜默认值；后续节点只读取这份冻结快照，规则更新必须新建版本并重新审批。
3. **确定性商家预筛**：招商 DB/Repository 按 tenant、类目、城市、黑白名单、报名系统和销售组织做可解释的硬条件过滤，输出候选商家及逐条命中/排除原因；敏感名单只暴露必要判定结果，不进入 prompt 原文。
4. **LLM 软条件排序与草案生成**：LLM 只在硬条件候选集内处理语义性/软条件、排序和理由，并生成 `CampaignDraft + CouponBatchDraft + MerchantShortlist`；领域 Service 再校验实体、规则引用、金额/折扣/时间和商家资格后持久化草案。LLM 不得放宽黑白名单或硬规则，也不直接写库或调用券系统。
5. **运营审核并投放招商**：`launch_approval` 展示规则快照、候选范围、活动/券批次草案、素材和规范化参数。审批通过后，独立幂等节点物化券批次并将招商活动投放到商家侧；拒绝/修改则产生新草案版本，旧审批失效。
6. **报名与商品圈选并行汇聚**：一条分支接收商家自主报名，另一条按规则自动圈选符合活动条件的商家商品；二者都执行商品范围、价格、类目、关键词、商家资格和 `BusinessConfirmationPolicy`，以业务唯一键去重后汇聚为 `EnrollmentItem`。工作流可在报名窗口内等待 webhook/轮询事件，不能用一个长时间占用的 LLM 调用等待。
7. **报名商品关联券批次**：领域 Service 在同一业务事务中校验活动、报名项和券批次版本/档位，建立 `EnrollmentCouponLink`；重复事件幂等，部分失败可重试并对账，不产生悬空关联。
8. **推送选品侧并招后选**：把已关联报名项通过 `submit_assortment` 推送到选品系统，按选品规则异步接收 `SelectionDecision`；外部请求、回执、拒绝原因和版本均落 execution ledger/outbox。未入选商品不得进入 C 端投放集合。
9. **C 端投放**：基于最终入选集合生成 `ConsumerPlacementDraft`；`consumer_publish_approval` 绑定选品结果、券关联和投放参数，批准后由 `publish_consumer_placement` 幂等执行。选品结果或参数变化会使旧审批失效；外部状态未知进入对账，不盲目重投。
10. **通知商家并闭环**：按商家维度发送入选/未入选、投放时间、优惠档位和可解释原因，保存送达回执；失败进入通知重试/死信，不回滚已完成的 C 端投放。运营侧同时获得汇总结果和可追踪的 `job_id/correlation_id`。

场景 A 串起：多入口身份映射 + 版本化 RAG 规则快照 + 确定性规则引擎 + 受限 LLM 决策 + 分支/汇聚 + 事件等待 + 动态业务确认链 + 双高风险 HITL + Checkpoint/Durable Job + Outbox/对账 + 审计与通知。Planner 只负责把已选活动模板实例化为预定图，不允许运行时自由改写上述安全边界。

**场景 B：运营实时归因排查（Agent / loop，步骤动态涌现）**

真实场景：一线运营 / 销售 / 分析师每日高频——某核心指标异常（如“昨天华东餐饮招商转化率为什么跌了”），需快速定位根因。步骤无法预先编排：查什么、下钻到哪、追哪条线索，完全取决于上一步发现。跨企业通用（电商 / SaaS / 金融 / 本地生活都有“指标异常归因”）。这正是 Agent loop（ReAct）+ orchestrator-workers + evaluator-optimizer 的用武之地，与场景 A 的预定 DAG 形成对照。

1. 用户自然语言提问：“华东餐饮招商昨天转化率为什么跌了？”
2. Guardrails + 权限：输入检测 + 能否看华东 / 招商数据。
3. Agent 动态规划（LLM 判断，非预设）：先查招商漏斗。
4. **［动态］调工具**：查招商漏斗 DMS → 发现“报名—核销”环节跌；看到结果，临时决定下钻。
5. **［动态］调工具**：按品类下钻 → “正餐”品类异常。
6. **［动态］调工具**：查正餐近期活动 → 某大活动刚结束。
7. **［动态］调工具**：RAG 查历史经验 / 政策 → 该品类活动结束历来致短期下滑。
8. evaluator-optimizer 自评：假设“活动结束导致”证据充分吗？补查大盘同比。
9. **［动态］调工具**：查大盘同比 → 非系统性，聚焦活动结束。
10. 综合推理：归因假设 + 证据链 + 已校准的不确定性/未校准标识 + 建议动作（跟踪 / 补活动）。
11. HITL（可选）：推结论到 IM 群 / 建跟踪任务前二次确认。
12. 全程 trace + Memory（支持多轮追问：“那北京呢？” / “再深挖客诉维度？”）。

**关键**：④—⑨每一步是 Agent 看到上一步结果后临时决定的，不是预设 DAG——这就是“动态执行节点任务”。

双场景对照：

| 维度 | 场景 A 招商活动 | 场景 B 归因排查 |
| --- | --- | --- |
| 模式 | Workflow（DAG，步骤预定） | Agent（loop，步骤涌现） |
| 步骤来源 | 模板预设（规则快照 → 商家筛选 → 活动/券草案 → 招商投放 → 报名/圈品 → 券关联 → 招后选品 → C 端投放 → 通知） | 每步由上一步发现动态决定 |
| 命中模式 | prompt-chaining / routing / parallelization / event wait + join | ReAct + orchestrator-workers + evaluator-optimizer |
| 何时用 | 流程已知、跨天、需持久化编排 | 探索性、实时、依赖中间发现 |

双场景 + Workflow-vs-Agent 架构决策，是面试 30 分钟故事的主轴。

### 5.1 场景 A 实现规格（ADR-028，实现方据此接线）

**V0.1 MVP 只读提案版**实现同一场景的只读纵向切片：用户描述招商需求 → `search_campaign_rules` 返回带逐字段引用的结构化规则 → `query_merchants` 按硬规则预筛 10 家样本商家 → LLM 只在候选集内处理软条件排序，输出 `CampaignProposal`（规则摘要、活动/券批次草案预览、推荐商家、理由、未决项和引用）。MVP 不持久化 Campaign/CouponBatch，不接真实 IM/报名/选品/C 端系统，不执行任何写工具。它与完整场景共用 Graph state、Tool/Retriever/Provider Protocol、规则 schema 和测试框架；V0.3 只扩展后续有副作用节点，不重写 MVP。

```text
V0.1：需求规范化 → 规则检索/快照预览 → 硬规则商家预筛 → LLM 软排序 → CampaignProposal + 引用
V0.3：V0.1 → 持久化活动/券批次草案 → HITL(launch) → 物化券批次/招商投放
      → [商家自主报名 || 系统自动圈品] → 业务确认链 → 报名商品关联券批次
      → 推送选品/等待结果 → HITL(consumer_publish) → C 端投放 → 商家通知/审计
```

`CampaignRuleSnapshot` 是场景 A 的关键输入契约。每个字段包含规范化值、`source_document_id/source_version/chunk_id`、提取置信/校验状态；整体包含 `snapshot_hash/effective_at`。硬规则由确定性 `EligibilityPolicy` 执行，LLM 只能输出 `eligible_candidate_ids` 的子集、排序和解释；遇到规则缺失、冲突或无法确定的软条件必须列入 `unresolved_items` 并 abstain，不能自行补全。黑白名单成员、内部销售组织详情等受限数据只以判定标签或脱敏摘要进入模型上下文。

V0.1 的 `search_campaign_rules` 可把规范化快照按 `tenant_id + snapshot_hash` 幂等写入平台派生缓存/Checkpoint，并只向模型返回不可猜 `rule_snapshot_id`、脱敏字段和引用；这仍属于可重算的只读查询投影，不是 Campaign/CouponBatch 业务副作用。`query_merchants` 从受信 Context 解析该 ID，调用方不能传任意规则覆盖。V0.3 在 `persist_campaign_draft` 时把同一 canonical snapshot 提升为 Business DB 中与 Campaign 绑定的不可变业务事实，且重新校验 hash/version/权限，不能信任 V0.1 缓存直接落库。

实现时至少建立以下类型化结构，不用一个自由形 `rules: dict` 承载全部业务语义：

```text
CampaignRuleSnapshot
├─ basic: template_ref / campaign_type / campaign_window / enrollment_window / product_scope
├─ recruitment_scope: categories / cities / allowlist_ref / denylist_ref /
│                       enrollment_systems / sales_org_scope
├─ enrollment_policy: mode[merchant|auto|hybrid] / linked_campaign_rules /
│                      accepted_sources / late_event_action[reject|new_version]
├─ product_circle_policy: policy_ref / policy_version / product_price /
│                        product_categories / keywords
├─ assortment_policy: policy_ref / policy_version /
│                      selector_mode[external|local_fixture] / completion_rule
├─ benefit_policy: tiers[base|boosted] × funding[fixed|stepped|discount_rate] /
│                  currency / rounding / budget_cap
├─ confirmation_policy: ordered_steps[merchant|sales|sales_manager] /
│                       timeout_action[reject|escalate|explicit_auto_confirm]
├─ merchant_material: title / hero_image_ref / introduction / tags
└─ field_evidence / snapshot_hash / effective_at

CampaignProposal
├─ intent / rule_snapshot_ref / campaign_draft / coupon_batch_draft
├─ hard_eligible_ids / ranked_merchants[{merchant_id, reasons, evidence_refs}]
└─ unresolved_items / warnings

ProductSnapshot
├─ product_ref / product_version / merchant_id / source_ref / captured_at
├─ category / normalized_price / currency / normalized_title / keyword_labels
└─ eligibility_facts
```

时间统一为带时区时间并校验 `enrollment_window ⊆ campaign lifecycle`；金额、阶梯和折扣用 Decimal 规范化，阶梯区间不得重叠，基础/膨胀档资金约束必须可判定。第 6 步自动圈品必须以受信商品库返回的 `ProductSnapshot` 和冻结的 `product_circle_policy_ref/version` 为输入，由确定性 `ProductEligibilityPolicy` 判定价格、类目、关键词和商品状态；LLM 不参与硬资格判定。第 8 步招后选品另外绑定 `assortment_policy_ref/version`，禁止用自动圈品规则代替最终选品规则。`hero_image_ref` 只允许受控 ObjectStore/素材库引用，不能让模型生成任意外链。

工具清单（注册到 `ctx.tools`，真实接入层 `tools/builtin/`，Mock 在 `tools/builtin/_mock/`）：

| 工具 | 签名 | 风险 / 节点类型 | 幂等与 Mock |
| --- | --- | --- | --- |
| `search_campaign_rules`（RAG） | `run({intent, effective_at})` | 低 / 自动只读，强制权限 pre-filter | 不允许调用方传 ACL；返回规则字段、版本和引用 |
| `query_merchants` | `run({rule_snapshot_id, limit})` | 低 / 自动只读 | EligibilityPolicy 硬过滤；样本商家与命中/排除原因 |
| `query_eligible_products` | `run({campaign_id, merchant_ids, rule_snapshot_id, product_circle_policy_ref, product_circle_policy_version, cursor?, limit})` | 低 / 自动只读 | 参数必须与规则快照交叉校验；ProductEligibilityPolicy 硬过滤；返回 ProductSnapshot、分页游标与命中/排除原因 |
| `persist_campaign_draft` | `run({proposal, rule_snapshot_id, idempotency_key})` | 中 / 自动写权限复核 | Campaign/CouponBatch 本地草案版本；不产生外部投放 |
| `materialize_coupon_batch` | `run({coupon_batch_draft_id, idempotency_key})` | 高 / `launch_approval` 后独立写入 | execution ledger；券批次/外部回执 |
| `publish_recruitment` | `run({campaign_id, merchant_scope_hash, material_version, idempotency_key})` | 高 / 同一 launch plan 批准后的独立副作用 | 投放 reservation + receipt + 对账 |
| `upsert_enrollment_items` | `run({campaign_id, source, items, idempotency_key})` | 中 / 自动写权限与确认链复核 | 自主报名/自动圈品统一业务唯一键 |
| `link_coupon_batch` | `run({enrollment_item_ids, coupon_batch_id, tier_mapping, idempotency_key})` | 中 / 领域事务写 | 无悬空关联；重复事件幂等 |
| `submit_assortment` | `run({campaign_id, enrollment_item_ids, assortment_policy_ref, assortment_policy_version, idempotency_key})` | 中 / 外部异步写，策略可升级 HITL | request/receipt；招后选品规则版本绑定；结果用 webhook/轮询事件恢复 |
| `publish_consumer_placement` | `run({selection_version, placement_spec, idempotency_key})` | 高 / `consumer_publish_approval` 后独立写入 | execution ledger + 对账；C 端投放回执 |
| `send_merchant_notification` | `run({merchant_id, result_version, template_id, channel?, idempotency_key})` | 中 / 自动或策略升级 HITL | merchant + result_version 去重；送达回执/死信 |

Golden 数据集：`tests/golden/scenario_a.jsonl`，初始不少于 30 条人工审阅样本；V0.1 覆盖六类规则字段、缺失/冲突规则、硬条件过滤、软条件排序、权限拒绝与引用，V0.3 追加商品快照/分页圈选、重复请求、动态确认链、审批过期/篡改、双报名来源汇聚、关窗/迟到事件、选品拒绝、恢复和工具失败。后续以真实失败案例持续扩充。每行使用与版本范围匹配的 schema，例如：

```json
{"input":"...","expected_rule_fields":["campaign_template","merchant_scope","enrollment_rules","benefit_tiers","confirmation_policy","merchant_material"],"expected_hard_eligible_ids":["..."],"expected_steps":["search_campaign_rules","query_merchants"],"must_hitl":[]}
{"input":"...","expected_steps":["persist_campaign_draft","hitl:launch","materialize_coupon_batch","publish_recruitment","query_eligible_products","enrollment_join","link_coupon_batch","submit_assortment","hitl:consumer_publish","publish_consumer_placement","send_merchant_notification"],"must_hitl":["launch","consumer_publish"]}
```

Eval 指标：① 规则字段完整率、冲突识别率与引用可回查率；② 硬规则资格准确率（必须 100%，LLM 不参与）；③ 软排序质量与 abstain 正确性；④ 步骤/分支汇聚命中率；⑤ 工具参数与 HITL/业务确认链触发正确性；⑥ 外部副作用幂等/对账结果；⑦ cost / latency SLO。质量、业务成功、成本和时延分开报告。

### 5.2 场景 B 实现规格

工具清单：

| Tool | 参数 | 用途 |
| --- | --- | --- |
| `query_funnel` | `run({dimensions})` | 查招商漏斗 |
| `drill_down` | `run({dim, value})` | 按维度下钻 |
| `query_activity` | `run({category?, merchant_id?})`（至少一个） | 查近期活动 |
| `query_market_overview` | `run({period})` | 大盘同比 / 环比 |
| `search_history_experience`（RAG） | `retriever.retrieve(query, ctx, query_filters)` | 历史经验 / 政策；ACL 由 Retriever 强制注入 |
| `save/search_memory` | memory-as-tool | 跨会话归因记忆 |

Golden 数据集：`tests/golden/scenario_b.jsonl`，初始不少于 50 条人工审阅样本，开发集与至少 20 条冻结 holdout 分离，覆盖可归因、证据不足、冲突证据、越权维度、注入文档与多轮追问。根因标签/golden rationale 与生产可查询数据分库存放，prompt、Retriever 和 Tool 不得读取标签。每行：

```json
{"input":"...","expected_outcome":"attributed","acceptable_hypotheses":["..."],"required_evidence":["..."]}
```

Eval 指标：① 归因准确率（固定 rubric 的人工标注 + LLM-as-judge 辅助）；② 引用证据支持率；③ 证据链充分性；④ abstain/coverage-risk；⑤ 置信度校准（样本足够时报告 Brier/ECE 或分桶准确率）；⑥ cost / latency SLO。模型自报 confidence 未校准前只作解释字段，不能用任意 `min_confidence` 充当质量门禁。LLM judge 固定模型、prompt、temperature 和数据版本，多次采样记录均值/方差，不作为唯一 PR 硬门禁。

两场景的确定性 CI 使用 `MockLLMProvider + _mock`，但不能据此声称真实模型、真实 Embedding 或企业接入通过。V0.1 必须另跑真实 DeepSeek smoke；后续每个真实 adapter 按详细执行路线保存独立验证证据。

### 5.3 招商领域模型与事务不变量（ADR-026）

核心实体：`Merchant`、`ProductSnapshot`、`CampaignRuleSnapshot`、`Campaign`、`CouponBatch`、`LaunchSagaState`、`RecruitmentPublication`、`Enrollment`、`EnrollmentItem`、`EnrollmentCouponLink`、`ConfirmationTask`、`AssortmentSubmission/SelectionDecision`、`ConsumerPlacement`、`MerchantNotification`。所有表含 `tenant_id`、业务主键、`version`（乐观锁）、创建/更新时间；外键或唯一约束必须包含 tenant，防止跨租户关联。商品可以来自企业商品库 Adapter，但 Oria 至少保存脱敏稳定引用、提交时版本与必要快照，保证审批和对账可复核。

- `CampaignRuleSnapshot`：对一次活动使用的结构化规则、逐字段来源引用和 `snapshot_hash` 做不可变快照；规则变化只能生成新版本，不能原地影响运行中的审批/报名。
- `ProductSnapshot`：以 `(tenant_id, product_ref, product_version)` 标识商品快照，只保存执行圈品规则和事后复核所需的脱敏字段；分页重试必须绑定同一商品库 snapshot/cursor，禁止把不同时点的页混成一次确定性圈选。
- `Campaign`：`draft → pending_launch_approval → recruiting → selecting → pending_consumer_publish → active → completed | cancelled`；非法跳转拒绝。
- `CouponBatch`：`draft → materializing → ready | failed | unknown → expired`；唯一键 `(tenant_id, campaign_id, coupon_spec_hash)`，launch 审批哈希必须覆盖规则、活动与券草案规范化参数。
- `RecruitmentPublication`：唯一键 `(tenant_id, campaign_id, merchant_scope_hash, material_version)`；记录商家侧招商投放 request/receipt 与对账状态，不与 C 端投放混用。
- `Enrollment`：唯一键 `(tenant_id, campaign_id, merchant_id)`；`EnrollmentItem` 唯一键 `(tenant_id, campaign_id, merchant_id, product_ref, product_version)`，并记录 `sources: set[merchant|auto]`。双来源命中同一商品时幂等汇聚，不重复报名。
- `ConfirmationTask`：按快照中的 `BusinessConfirmationPolicy` 生成 merchant/sales/sales_manager 步骤、顺序、期限和 timeout action；确认主体必须来自可信目录，不能由 LLM 指定。
- `EnrollmentCouponLink`：唯一键 `(tenant_id, enrollment_item_id, coupon_batch_id, benefit_tier)`；创建前校验三方版本与状态，禁止悬空或把未确认商品关联到可用券批次。
- `AssortmentSubmission/SelectionDecision`：以 `(tenant_id, campaign_id, submission_version)` 关联外部选品请求与逐商品决定，必须绑定独立的 `assortment_policy_ref/version`；只有 `selected` 且券关联有效的条目可进入投放集合。
- `ConsumerPlacement`：唯一键 `(tenant_id, campaign_id, selection_version, placement_spec_hash)`；记录 C 端外部 request/receipt 和对账状态，审批哈希必须覆盖最终选品版本与投放参数。
- `MerchantNotification`：唯一键 `(tenant_id, merchant_id, campaign_id, result_version, template_id, channel)`；通知失败进入重试/死信，不回滚业务结果。
- 业务状态写入、`tool_executions` 与 outbox event 在同一数据库事务提交；外部券、招商、选品、C 端投放和 IM 调用采用 reservation + idempotency key + receipt reconciliation。跨系统不存在分布式事务时，以状态机、Outbox、幂等消费和对账收敛，不宣称全链路原子。

LLM 只能把已授权候选做软条件排序并生成草案/解释，不能直接构造数据库写入，也不能决定硬资格、审批主体或最终选品结果；所有写入经领域 Service/EligibilityPolicy 校验金额、折扣、日期、区域、黑白名单、销售组织、商家/商品状态、活动状态、确认链和权限不变量。

### 5.4 RAG、记忆与内容安全边界（ADR-011 / ADR-012 / ADR-027）

- 文档摄入时写入 `tenant_id/document_id/version/source_uri/owner/ACL/data_classification/content_hash`；更新/删除必须传播到 chunk 与向量索引。
- `documents/document_versions/ingestion_runs` 关系目录是知识生命周期真相源，原文在 ObjectStore；Chroma/Milvus 只保存可重建 chunk/vector 投影。索引损坏或迁移时必须能按 catalog + content hash 重建，不能把向量库当唯一文档库。
- `AuthorizedRetriever` 从 PolicyDecision 生成不可覆盖 ACL filter，并在返回后再次校验 tenant/document version；答案必须携带 chunk/document 引用与版本。
- 招商规则文档必须带 `rule_type/effective_from/effective_to/priority/supersedes/template_ref` metadata；多来源优先级由确定性 RuleResolver 按显式制度计算，不由 LLM 选择“更可信”的版本。无法唯一解析时 `CampaignRuleSnapshot` 标记冲突并阻断有副作用节点。
- 用户、RAG 文档、Tool/MCP 输出都标记 trust level；检索内容一律视为不可信数据，不获得 system/tool 指令权。
- 跨会话记忆默认 opt-in，按 tenant + subject namespace 隔离，支持 TTL、查看、删除和导出；记录 provenance/confidence/sensitivity，敏感或低置信内容不得自动长期保存。
- 权威业务事实从业务系统读取；模型生成的 episodic memory 不能覆盖权威事实，使用前经过注入/投毒检测和权限过滤。
- 仓库 fixture/评测集只使用合成或有权使用且已脱敏的数据；随数据记录来源、许可证、生成器 seed、schema/version 和污染检查。禁止将真实客户/商家数据、根因标签或 golden rationale 混入可检索语料。
- 记忆删除必须同步删除正文、向量和缓存；审计仅保留脱敏删除事件与对象 ID/hash，不保留被删除正文。Checkpoint/备份可能存在的残留按公开的保留、到期和恢复删除策略处理，不能笼统承诺“立即物理清零所有副本”。

---

## 六、技术选型（双轨制：社区版低成本跑 / 正式版企业接入，每个组件留 seam）

项目服务企业用户，但社区版首先服务“下载后快速验证完整流程”。原则：每个存储 / 能力都有 Protocol seam；社区默认不要求云账号、外部数据库、Collector 或真实 IM，正式版经配置 + entry-point 接入企业实现。社区版是长期维护的可跑版本，非临时脚手架；社区 → 正式的切换写 ADR 当面试故事（ADR-002 / 003 等）。

| 组件 | 社区版（OSS 低成本） | 正式版（企业接入） | seam（Protocol / 实现位置） |
| --- | --- | --- | --- |
| 向量库 | Chroma（本地嵌入式） | Milvus / 云向量库 | `Retriever + Embedder`（§3.1） |
| 平台 DB（会话 / Checkpoint / 事件） | SQLite + 官方 `AsyncSqliteSaver` / Supabase Postgres（可选） | PostgreSQL + 官方 `AsyncPostgresSaver` | Saver factory + Repository + Outbox（§3.2 / §七） |
| 招商业务 DB / 商家商品源 | SQLite/JSON 种子数据（默认）；Supabase 可选 | 美团 DMS / MySQL / 商品库 | Domain Repository + Tool（§5.1 / 5.2） |
| 招商外部业务系统 | Mock 券批次/商家侧/报名/选品/C 端投放 Adapter | 企业券、招商、报名、选品、C 端投放系统 | 场景 A Tool + execution ledger/outbox（§3.7 / §5.1） |
| 缓存 | 内存 dict | Redis | `CacheStore`（§3.6） |
| 对象存储 | 本地目录 | MinIO / S3 | `ObjectStore`（§3.6） |
| Logs / Traces / Metrics | Console JSON 默认；OTel console/OTLP 可选，不要求 Collector | OTLP → 企业可观测平台；可选 Langfuse | 分离的 Logger / Tracer / Meter（`obs/`） |
| LLM | `MockLLMProvider` + 用户自带 key（DeepSeek 等） | 同 + 企业网关 | `LLMProvider`（§四） |
| IM 入站 / 通知 | CLI + `_mock`（飞书 / 钉钉入站，大象 / 飞书 / 钉钉出站） | 真实飞书 / 钉钉 webhook 与大象 / 飞书 / 钉钉通知 | `IngressAdapter + Notifier`（§3.5） |
| Embedding | FixtureEmbedder（demo）/ BGE 本地（standard） | 智谱 / OpenAI / 企业 Embedding | `Embedder`（§3.1） |

运行组合：

| 组合 | 目的 | 外部依赖 | 允许声明的结论 |
| --- | --- | --- | --- |
| `community + demo` | 5 分钟内验证 Oria 流程 | 无；MockLLM + fixture/local data | 只能证明流程、契约和确定性测试通过 |
| `community + standard` | 社区用户体验真实模型/RAG | 用户 LLM Key；BGE 首次下载 | 可证明指定真实模型 smoke 与本地 RAG 结果 |
| `production + standard` | 企业接入 | 企业 DB/LLM/IM/OTLP 等 | 只有对应 adapter 集成测试和回执齐全才可声明通过 |

**四层数据后端**（以招商查询工具为例）：① `_mock/fixture`（CI）；② SQLite/JSON 种子数据（社区默认）；③ Supabase（社区可选集成）；④ DMS/MySQL（企业生产）。四者使用同一 Domain Repository + Tool Protocol，上层 Graph 不分叉。

**迁移面试故事**：Chroma → Milvus、SQLite → PostgreSQL、Supabase → DMS 各一条 ADR（ADR-002 / 003 / 023），讲“为什么换、踩什么坑”。SQLite → PostgreSQL 默认只迁移 Oria 自有 platform/business 表；Chroma 不是真相源，由 catalog + ObjectStore + 锁定 Embedder revision 重建 Milvus 影子索引并对照后切换。官方 SQLite/PostgreSQL saver 的内部表不纳入 Alembic 搬迁；默认在切换前停止接单并排空非终态 Job，新任务在 Postgres saver 建立 checkpoint。默认回滚只在“仍停止接单、新库未开放写入”的验收窗口内执行；开放新库写入后若无反向 CDC/对账方案，不得声称可无损回滚。在途 checkpoint 只能经官方 saver API 导出/导入且通过 compatibility suite 后单独启用，否则明确不声称已迁移。

**配置切换**：见 §4.3 `edition + runtime_profile` 和各 backend 项。Factory 必须有契约测试证明替换实现不改变上层调用；production 缺配置时 fail closed，不得静默降级成本地 Mock。

---

## 七、数据库设计（目标栈）

**PostgreSQL（社区 SQLite 使用同一逻辑分域）**：

- **Platform schema/DB**：`tenants / subjects / roles / role_bindings / policies`、`documents / document_versions / ingestion_runs`、`sessions`、`jobs / job_events`、`approvals / external_waits / integration_event_inbox`、`webhook_endpoints / webhook_deliveries`、`eval_datasets / eval_runs`，以及与平台状态同事务的 `audit_events / outbox`。
- **Business schema/DB**：`merchants / product_snapshots / campaign_rule_snapshots / campaigns / coupon_batches / launch_saga_states / recruitment_publications / enrollments / enrollment_items / enrollment_coupon_links / confirmation_tasks / assortment_submissions / selection_decisions / consumer_placements / merchant_notifications`、`tool_executions`、以及与业务状态同事务的 `domain_events / audit_events / outbox`。
- **Saver-owned schema**：LangGraph 官方 checkpoint/writes/metadata；由对应 saver setup/migration 管理，Oria 不自定义精简字段替代，也不纳入 Oria Alembic revision。
- 身份表映射 human actor 与 service executor；认证来自 OIDC/SSO/workload identity 或本地开发身份，Oria 不保存企业密码。跨 platform/business DB 无法使用数据库外键时，Service 必须校验 tenant/resource 引用并用对账测试保证一致。两库的 Audit/Outbox 共用 schema contract 但使用独立表和 revision；统一查询由幂等消费器生成可重建投影，不引入跨库事务。

**Redis**：热数据与限流；语义缓存 key 必须包含 tenant、权限/知识版本、模型与 prompt 版本，命中后仍执行输出安全检查。只缓存只读、可重算答案，禁止缓存审批决定、写工具结果、包含敏感数据的响应或把缓存命中当作权威业务事实。Redis 锁只作优化，不作为 Job 领取、幂等或资金/券一致性的唯一保障。

**Milvus**：知识库向量集合（`tenant_id, chunk_id, embedding, document_id, document_version, ACL, classification, metadata`）；pre-filter 强制 tenant + ACL，召回后再次校验。

**S3 / MinIO**：原始文档（招商规则、历史活动文档）。

> **统一关联模型**：`tenant_id/session_id/thread_id/run_id/job_id/correlation_id` 贯穿 API、Job、Graph、LLM、Tool、Checkpoint、Event 与 Trace。Checkpoint 与 Domain/Audit Event 各自权威，按 §3.2 的一致性边界关联。

> **多租户（企业底线）**：所有业务表、Job、审批、工具账本、事件与向量 metadata 均含 `tenant_id`；查询 deny-by-default，正式版数据库启用 RLS/等价隔离，复合外键不得跨 tenant。覆盖跨租户 ID 猜测、缓存污染、向量泄漏、checkpoint 越权和 webhook 越权测试。配置 secret 加密，传输使用 TLS；明确 checkpoint、日志、记忆、文档的 retention/export/delete 策略。

---

## 八、项目目录结构

```text
oria/
├── Oria架构设计.md           # 架构目标、边界与高层路线（本文）
├── docs/
│   ├── Oria详细执行路线.md     # 任务、真实验证场景、测试和证据门禁
│   └── adr/                   # 架构决策记录（面试 hook 来源）
├── src/
│   └── oria/
│       ├── core/                  # Context / EventBus / middleware / Protocol / registry
│       │   ├── context.py
│       │   ├── events.py          # agent/capability 实时事件 + outbox envelope
│       │   ├── middleware.py
│       │   ├── protocols.py
│       │   └── registry.py
│       ├── providers/             # OpenAI-compatible / Anthropic / Mock
│       │   ├── base.py
│       │   ├── openai_compat.py
│       │   ├── anthropic.py
│       │   └── mock.py
│       ├── prompts/               # Jinja 模板与版本注册
│       ├── resources/demo_data/   # wheel 内置合成规则、商家种子与数据 manifest
│       ├── domain/                # 实体/状态机、Service 与 Repository Protocol（无 DB client）
│       ├── storage/               # SQLAlchemy/Chroma/Milvus/Redis/ObjectStore adapters
│       │   ├── database.py        # engine/session 与 tenant transaction context
│       │   ├── repositories.py
│       │   ├── vector.py
│       │   ├── cache.py
│       │   └── objects.py
│       ├── migrations/            # wheel 内两条 Alembic revision 链
│       │   ├── platform/
│       │   └── business/
│       ├── tools/
│       │   ├── base.py            # ToolPolicy + registry + execution ledger
│       │   ├── router.py
│       │   ├── builtin/           # 招商/券/报名/选品/投放/DMS/Raptor/Watson/IM + _mock
│       │   │   └── im/
│       │   ├── mcp_client.py
│       │   └── mcp_server.py
│       ├── memory/                # conversation / longterm / memory-as-tool
│       ├── rag/                   # ingest / authorized retrieve / rerank
│       ├── agent/                 # ReAct + supervisor + subagents
│       ├── orchestrator/
│       │   ├── dag.py
│       │   ├── patterns.py        # 5 种 workflow 模式
│       │   ├── checkpoint.py      # 官方 saver factory + adapter
│       │   └── hitl.py
│       ├── guardrails/
│       ├── permission/            # PolicyEngine：tenant + RBAC + ABAC
│       ├── jobs/                  # DB 状态机 + worker + webhook delivery
│       ├── obs/                   # logs / traces / metrics / cost
│       ├── eval/                  # datasets / regression / judge harness
│       ├── ingress/               # CLI/飞书/钉钉入站验签、身份映射与 CampaignIntent
│       ├── api/                   # FastAPI v1 + SSE + approvals
│       └── cli.py
├── tests/                     # unit / contract / integration / security / recovery / golden
├── eval/                      # versioned datasets / baselines / CI gate & nightly budget config
├── web/                       # React/TypeScript/Vite；独立 package-lock 与 Playwright
├── alembic-platform.ini       # 开发入口；CLI 仍是统一升级入口
├── alembic-business.ini
├── examples/                  # 面向读者的调用示例；不作为运行时资源真相源
├── reports/verification/      # 脱敏验证报告；按 version/run_id 归档
├── .gitignore                 # 忽略 .oria-data/.artifacts/.venv/缓存与构建产物
├── pyproject.toml
└── README.md
```

`Oria架构设计.md` 保持为仓库根目录的稳定入口，避免外部引用和执行前置检查失效；若未来确需改名或迁移，必须在同一提交中更新 `AGENTS.md`、README、详细路线和全部链接，并在原路径保留明确的迁移指引。

---

## 九、增量执行路线（高层）

完整范围不删减，按“永久纵向切片 → 能力增强 → 企业化 → 生态与生产证明”递进。详细任务、前置检查、真实验证场景、测试用例和证据要求见 [`docs/Oria详细执行路线.md`](docs/Oria详细执行路线.md)；每个执行任务开始前必须先检查该文档，不得绕过阶段准入条件。

| 版本 | 核心范围 | 可运行交付 |
| --- | --- | --- |
| V0.1 MVP | 场景 A 只读提案版；结构化规则快照预览 + 硬规则商家预筛 + LLM 软排序/活动券草案预览；2 个只读工具；社区双 profile | 零配置、零副作用 demo + 真实 DeepSeek smoke |
| V0.2 RAG/Provider | 五家 Provider profile、混合检索、rerank、Principal/PolicyEngine read ACL、引用与 RAG Eval | 经真实授权过滤且可量化的本地 RAG |
| V0.3 场景 A 完整版 | 活动/券批次草案、写操作 RBAC/职责分离、招商审核投放、双来源报名/圈品、动态确认链、券关联、异步招后选、C 端投放、通知、Checkpoint/HITL/幂等/审计 | 可中断恢复、可对账且严格区分招商投放与 C 端投放的完整 Workflow |
| V0.4 场景 B | 动态归因 Agent、冻结 holdout、防标签泄漏、evaluator-optimizer、工具证据链 | 可复核的单 Agent 归因闭环 |
| V0.5 多智能体/记忆 | Supervisor/Subagent、上下文压缩、memory-as-tool、完整 ABAC/Guardrails、等额预算盲评 | 公平的单/多 Agent 对照实验 |
| V0.6 平台服务化 | FastAPI、OIDC/JWT、Durable Job、PostgreSQL 多 worker、SSE、Webhook、审批与取消 | 经身份隔离的跨进程、跨天任务闭环 |
| V0.7 生态扩展 | MCP 2026 capability 探测、受信插件/不受信进程隔离、受限 Redis 缓存、外部 Provider/Tool/Retriever | 扩展受控注册且不夸大 SDK 能力 |
| V0.8 生产证明 | 非空 Oria 数据 SQLite→PostgreSQL 迁移、catalog→Milvus 重建与 saver 可验证切换、OTel、完整 Eval、SBOM/供应链、安全/恢复/压力测试、React Web UI、Docker | 可复现旗舰演示与面试证据 |

所有版本同时运行三类验证：确定性 Mock/fixture CI、社区真实组件验证、适用时的企业 adapter 集成验证。三类结果必须分开记录，Mock 通过不得写成“真实模型/真实企业接入通过”。

---

## 十、面试 hook → ADR 映射

每条 hook 对应一个 ADR + 一处实现，面试时能讲“为什么”。ADR 状态和文件索引以 [`docs/adr/README.md`](docs/adr/README.md) 为准；表中尚未实体化的 ADR 为“计划中”，不得被引用为已接受的决策证据。

| Hook | ADR | 要点 |
| --- | --- | --- |
| 为什么自己写 Provider，不用 LiteLLM | ADR-001 | 归一化 + 学习价值 |
| 为什么 Chroma → Milvus | ADR-002 | 规模 / HNSW / 分布式 |
| 为什么 SQLite → PostgreSQL | ADR-003 | 并发 / JSON / 审计量 |
| Checkpoint 怎么做幂等可恢复 | ADR-004 | 官方 saver + pending writes + resume/fault injection |
| NodeResult 与 Job Schedule 为什么分离 | ADR-005 | 单次执行状态 vs 批量/周期/常驻调度 |
| 何时用 Workflow / 何时用 Agent + 5 种模式 | ADR-006 | 确定性优先 + 模式库 |
| RAG vs fine-tune | ADR-007 | 数据时效 / 成本 |
| reranker 是否真正提升 | ADR-008 | 固定数据集前后对照；只展示真实 eval run 数字 |
| 多智能体成本与边界 | ADR-009 | 何时拆 Agent |
| Guardrails 架构（输入 / 输出 / 工具） | ADR-010 | 横切层 + 注入攻防 |
| RAG pre-filter 权限过滤 | ADR-011 | 召回前 metadata filter |
| memory-as-tool vs 背景摘要 | ADR-012 | 主动记忆 + 用户级跨会话 |
| 流式端到端设计 | ADR-013 | Provider → Loop → SSE |
| 异步 job 模型 | ADR-014 | 跨天长任务 + actor/executor 身份恢复 + webhook |
| Eval 独立子系统 + CI 门禁 | ADR-015 | 回归基线 |
| 借鉴 DeepSeek Harness 做插件化（薄核心 + seam） | ADR-016 | 为什么不从 day 1 全插件 |
| Context + Protocol vs DI 容器 | ADR-017 | service-location 显式好讲 |
| 为什么拆分 Checkpoint 与 Domain/Audit Event | ADR-018 | 执行恢复语义 vs 业务/合规事实；Outbox 保一致 |
| 插件 vs MCP 分层 | ADR-019 | 内部扩展 vs 外部协议 |
| 多租户隔离 | ADR-020 | actor/executor 双主体 + tenant deny-by-default + RLS/复合约束 + 跨租户测试 |
| 成本预算门禁 | ADR-021 | 单会话超限转 HITL |
| Prompt 版本管理（够用级） | ADR-022 | Jinja 模板 + 目录版本，非 A/B 平台 |
| 为什么 Supabase → DMS | ADR-023 | 社区种子数据 → 生产真实，同 Tool 切实现 |
| HITL 后副作用如何保证只执行一次 | ADR-024 | args hash + execution ledger + 幂等/对账 |
| Durable Job 如何崩溃恢复 | ADR-025 | DB 状态机 + lease epoch/fencing + accepted checkpoint + webhook delivery |
| 为什么需要招商领域模型 | ADR-026 | LLM 只提案；领域 service 守住业务不变量 |
| RAG/Memory 如何防注入与投毒 | ADR-027 | trust label + provenance + ACL + 生命周期治理 |
| 为什么硬资格不用 LLM 直接判定 | [ADR-028](docs/adr/ADR-028-deterministic-eligibility-and-llm-ranking.md) | EligibilityPolicy / ProductEligibilityPolicy 确定性过滤；LLM 仅做候选集内软排序/解释 |
| 外部报名/选品事件如何跨进程恢复 | [ADR-029](docs/adr/ADR-029-external-event-wait-and-resume.md) | waiting_event + event inbox + event envelope + wait/resource/version/checkpoint 绑定 |
| 跨 seam 值为什么校验后深度冻结 | [ADR-030](docs/adr/ADR-030-deep-immutable-seam-values.md) | 容器深度不可变 + 标准 JSON 投影；保护参数哈希、授权与重放稳定性 |

---

## 十一、自建原则

领域逻辑、适配层、数据集、策略与集成代码在本仓库实现，不复用任何外部 demo 项目代码（promo-mind、CodeScope 等仅可作设计参考）。LangGraph saver、MCP SDK、OTel 等基础设施遵循“复用成熟协议语义、不重写脆弱底层”的原则：

| 组件 | 自建位置 | 说明 |
| --- | --- | --- |
| Checkpoint 接入与恢复测试 | `src/oria/orchestrator/checkpoint.py` | 复用官方 saver；自建 tenant/trace/serializer adapter、状态 schema、故障注入与兼容测试 |
| HITL 审批与副作用账本 | `src/oria/orchestrator/hitl.py` | 基于 LangGraph interrupt，自建审批绑定、策略复核、幂等与对账 |
| Eval 子系统 | `src/oria/eval/` | 自建场景数据、指标注册、回归门禁和 judge harness |
| MCP client + server | `src/oria/tools/mcp_client.py` | 基于官方 SDK 对外暴露/消费 MCP；自建 Oria Tool/Policy adapter，不手写协议 framing |
| 招商 Tool 与领域接口 | `src/oria/tools/builtin/` | 商家/商品、券批次、招商、报名、选品、C 端投放、DMS/Raptor/Watson/IM 封装及 Mock 在本仓库实现，写入经 domain service |

> 可借鉴已有 demo 的设计思路，但代码全部重新实现，保证能讲透每个设计决策。

---

## 十二、插件化扩展层（借鉴 DeepSeek Harness）

> DeepSeek Harness（dsh）基于 Cordis，核心理念“一切皆插件”。Oria 借鉴其**薄核心 + 能力 seam + 可逆效应 + 类型化事件**，但不照搬 session-log/checkpoint 语义：执行恢复使用 LangGraph saver，业务/审计事件使用 Event + Outbox。

Python 映射（dsh 是 TS / Cordis，Oria 使用 Python 等价实现）：

| dsh 概念 | Oria 实现 |
| --- | --- |
| 共享 Context（`ctx`） | `core/context.py`：进程级 RuntimeServices + 每次执行 Context；只读转发服务，不共享 actor/run 状态 |
| 能力 seam（定义 / 提供 / 消费） | `typing.Protocol`（接口）+ Provider 注册 + consumer 注入 |
| 可逆效应（unwind on unload） | async 插件 `setup` 注册 teardown，使用 `contextlib.AsyncExitStack` 管理 |
| 类型化事件 | `core/events.py` EventBus：agent/capability 实时事件；domain/audit 经 outbox 持久化 |
| turn waterfall 中间件 | `core/middleware.py`：pre-step / request / llm-stream / tools pre-execute-post / turn-stopping |
| 插件发现（`dsh-plugin` 标签） | `pyproject.toml` entry points（`oria.plugins` group）+ `importlib.metadata`；支持本地插件目录 |

> **两层接缝澄清**：turn 中间件不另起执行循环，而是经 LangGraph callback / 在节点内包裹 `ctx.llm / ctx.tools` 调用实现（pre-step / request / llm-stream / tools pre-execute-post / turn-stopping）。dsh 的 turn 模型不照搬，只取横切拦截语义。

扩展点（插件可注册的服务）：

| 扩展点 | Context key | 内建实现 |
| --- | --- | --- |
| LLM Provider | `ctx.llm` | DeepSeek / OpenAI / Kimi / 智谱 / Claude |
| Tool | `ctx.tools` | 招商查询 / 活动券草案 / 招商投放 / 报名圈品 / 选品 / C 端投放 / IM 通知 / DMS / Raptor / Watson |
| Retriever（RAG） | `ctx.retriever` | Chroma / Milvus / BM25 |
| Embedder | `ctx.embedder` | BGE / 智谱 |
| Memory Store | `ctx.memory` | 短期 / 长期 / 向量 |
| Workflow Node | `ctx.nodes` | 自动节点 / 人工节点 + 自定义 |
| Guardrail | `ctx.guardrails` | 输入检测 / 输出过滤 |
| Subagent | `ctx.agents` | supervisor 子 Agent Provider |
| Ingress Adapter | `ctx.ingress` | CLI / 飞书 / 钉钉入站规范化与身份映射 |
| Notifier（IM） | `ctx.notifier` | 大象 / 飞书 / 钉钉 |

**插件 vs MCP 分层**：MCP 是外部工具协议（跨进程、标准化、跨系统）；Oria 插件是内部扩展机制（同进程、类型化、启动时发现）。两者互补——`mcp_client` 本身是一个 Oria 插件。Python entry point 不承诺真正热插拔：安装、卸载、升级后受控重启；manifest 声明 API version/permissions，生产环境按签名或 allowlist 加载，依赖冲突/初始化失败必须隔离并回滚注册。

> **信任边界**：entry-point 插件与 Oria 同进程、拥有 Python 代码执行权，只允许管理员安装的受信代码；manifest/签名不能形成真正沙箱。第三方或不受信扩展必须通过独立进程/容器的 MCP 接入，并由网络、凭证、资源限额和 ToolPolicy 约束，不能作为 entry-point 加载。

> **A2A 协议（future，仅认知）**：Google Agent-to-Agent 互操作标准，解决跨 Agent 协作。Oria 暂不实现，但插件 seam（`ctx.agents`）已为后续对接留口。面试话术：了解 A2A 与 MCP 的层次差异——MCP = 工具协议，A2A = Agent 间协议。

**实现边界（能力全部交付，但按版本逐层落地）**：

- 不强求从 day 1 “一切皆插件”。核心模块先内建实现，但都通过 **Context + Protocol** 暴露 seam（可替换）。
- 插件口子先打通 3 点：LLM Provider（已多模型）、Tool（注册表即插件雏形）、Retriever（RAG 后端可切 Chroma / Milvus）。
- 事件总线 + turn 中间件在 V0.5 完整化，trace / guardrails 是事件 / 中间件消费者。
- Entry-point 外部插件发现 V0.7 加入，演示“安装一个 pip 包，受控重启后多一个 tool / provider”。
- Checkpoint 与 Domain/Audit Event 按 §3.2 分工，通过统一关联 ID 与 Outbox 协作，不互相替代。

---

## 十三、起步（实现方据此开始）

1. 从 `docs/Oria详细执行路线.md` 的 `V0.1-T01` 开始，按 Depends on 拓扑顺序建立永久纵向切片；不得把整版任务一次性混在一个不可验证的大提交中。
2. 每次任务前先检查 `docs/Oria详细执行路线.md`，按版本准入、真实场景、测试和证据门禁推进；Fixture、Live、Enterprise 结果分开记录。
3. 符号 / 版本口径：LangGraph 等迭代快的库，安装后核对符号名，以所装版本为准（§0.5）。

---

## 十四、官方实现基线（2026-08-26 核验）

- LangGraph Persistence：<https://docs.langchain.com/oss/python/langgraph/persistence>
- LangGraph Graph API / reducer：<https://docs.langchain.com/oss/python/langgraph/graph-api>
- LangGraph Interrupt / idempotency：<https://docs.langchain.com/oss/python/langgraph/interrupts>
- LangGraph v1（`create_react_agent` 已弃用）：<https://docs.langchain.com/oss/python/releases/langgraph-v1>
- MCP 2026-07-28 变更：<https://blog.modelcontextprotocol.io/posts/2026-07-28/>
- MCP Python SDK v2：<https://pypi.org/project/mcp/>；未实现扩展以 <https://github.com/modelcontextprotocol/python-sdk/blob/main/ROADMAP.md> 为准
- DeepSeek 当前模型/退役公告：<https://api-docs.deepseek.com/updates/>；Chat JSON mode：<https://api-docs.deepseek.com/guides/json_mode/>；Responses JSON Schema：<https://api-docs.deepseek.com/api/create-response/>；Kimi 模型列表：<https://platform.kimi.com/docs/models>
- Anthropic Models API：<https://platform.claude.com/docs/en/api/models>；Structured outputs（native `output_config.format` + strict tool use）：<https://platform.claude.com/docs/en/build-with-claude/structured-outputs>
- OpenTelemetry GenAI semantic conventions：<https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/>
- OWASP Agentic Application Security：<https://genai.owasp.org/>

实现时把依赖版本锁入 `uv.lock`，并在 ADR 记录核验日期。若官方接口与本文示例冲突，以锁定版本的官方接口和契约测试为准；升级依赖必须先通过 Provider、Saver、MCP 与恢复兼容测试。
