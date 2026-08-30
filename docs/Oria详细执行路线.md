# Oria 详细执行路线

> 本文是 `Oria架构设计.md` 的执行配套文档。架构主文档定义目标和边界，本文定义实施顺序、阶段准入、真实验证场景、测试用例与证据。每次开始任务前必须先检查本文。仓库现已完成 `V0.1-T01–T10`、`V0.2-T01–T06` 与 `V0.3-T01`；`V0.1-Core`、`V0.2-Core`、真实 DeepSeek Nightly 和各自必需 Live 卡均已通过。当前口径是“V0.3 进行中（T01 完成）”，不包含 V0.2 企业 Adapter、DeepSeek 以外 Provider Live 或 V0.3 完整 Workflow。其余任务与验证状态以 §三总览和对应版本的“验证状态”小节为准。

> **场景 A 设计评审记录（2026-08-26）**：已按“需求接入 → 规则快照 → 商家预筛/LLM 软排序 → 活动与券批次草案 → 运营审核/招商投放 → 自主报名或自动圈品 → 券关联 → 招后选品 → C 端投放 → 商家通知”同步架构与路线。此次只更新设计契约，未生成代码、未运行验证，版本状态仍为“未开始”。

> **资深 Agent 架构复核修正（2026-08-27）**：已补齐 ProductSnapshot/ProductEligibilityPolicy，并分离 `product_circle_policy` 自动圈品规则与 `assortment_policy` 招后选品规则；冻结 `merchant|auto|hybrid` 报名模式与关窗语义、标准化外部事件契约、ToolPolicy/HITL 决策矩阵、LaunchPlan saga 补偿/对账语义和 ADR 索引。同步修正 V0.3/V0.6/V0.8 任务依赖与测试门禁；本次仍是文档设计修正，不代表代码或 Live 验证已通过。

## 一、执行与真实性规则

### 1.1 不可违反的原则

1. V0.1 是永久纵向切片，后续只扩展，不重写为另一套 Runtime。
2. Mock/fixture 只能证明流程和契约，不能证明真实模型、真实 Embedding、真实数据库或企业系统接入。
3. 真实验证必须记录实际 provider/model、数据版本、命令、时间、结果和原始证据；未执行不得写“已通过”。
4. 指标必须来自保存的 eval run，不预填“提升 10%”“准确率 90%”等结果。
5. 失败记录不得删除或只保留成功截图；修复后追加新的 run，并保留失败原因和关联提交。
6. 企业 Adapter 没有真实凭证或环境时可以完成 Mock/契约测试，但状态必须标为“未 Live 验证”。
7. 每个阶段分为 Core Gate 与外部验证卡：Core Gate 通过后可进入下一阶段实施；明确列为必需的 Live/E-like/E 卡未通过时，不得把该版本标为“全部验证通过”。可并行开发，但不能越级宣称完成。
8. 每个任务必须有唯一 ID、`Depends on`、可定位产物和可判定验证；依赖未完成时只能做不落主线的探索，不得把探索结果当成任务完成。

### 1.2 验证等级

| 等级 | 运行对象 | 能证明什么 | 不能证明什么 |
| --- | --- | --- | --- |
| F：Fixture | MockLLM、FixtureEmbedder、Mock Tool、固定数据 | 控制流、类型契约、错误处理、确定性回归 | 真实模型质量、真实外部服务 |
| C：Community Real | 本地 BGE、Chroma、SQLite、真实进程/网络回环 | 社区版真实本地链路、恢复与数据一致性 | 企业规模、企业系统兼容性 |
| L：Live Provider | DeepSeek/Claude 等真实公开 API | 指定日期、模型和配置下的真实模型调用 | 其他模型或未来版本表现 |
| E-like：本地企业栈 | 本地容器中的 PostgreSQL/Milvus/Redis/OTel 等 | 指定版本组件的多进程、迁移和故障语义 | 企业网络、SSO、网关、SLA 或真实业务系统 |
| E：Enterprise | PostgreSQL/Milvus/Redis/企业网关、DMS、IM 等真实环境 | 对应 Adapter 的真实集成结果 | 未接入的其他企业环境 |

### 1.3 测试类型

- `UT`：纯函数、领域不变量和 reducer 单元测试。
- `CT`：Provider/Tool/Retriever/Repository/Saver 契约测试。
- `IT`：真实本地组件或容器集成测试。
- `E2E-F`：完全确定性的 Fixture 端到端测试。
- `E2E-L`：真实公开模型端到端 smoke/eval。
- `SEC`：权限、注入、数据泄漏、路径/网络边界测试。
- `REC`：进程退出、重试、恢复、幂等和对账测试。
- `PERF`：延迟、吞吐、Token 和成本基线；先测量再定阈值。

pytest marker 固定为 `unit / contract / integration / live / enterprise / slow / security / recovery / performance`。默认 PR CI 禁止外网，仅运行 unit、contract 和不依赖外部服务的 integration；`live/enterprise/performance` 必须显式选择、配置资源或成本预算并输出独立报告，不能因缺 Key/组件被静默 skip 后仍把对应阶段标为通过。

#### 1.3.1 LangGraph 图测试模式

图测试只断言公开 state/event/interrupt 和领域副作用，不绑定 LangGraph 私有内部对象；锁定版本符号变化时只改测试 adapter。固定采用三层：

1. **Graph-UT（路由）**：builder 接受可注入的 fake/spy node callable，在编译前替换 model/tools/validate/HITL 节点；使用锁定版本官方内存 saver（当前预期 `InMemorySaver`，若实际版本名为 `MemorySaver` 则由薄 fixture 适配）执行最小输入，断言访问节点、条件边、终止边和未访问节点。禁止编译后 monkeypatch LangGraph 私有结构。
2. **Graph-E2E-F（最终 state）**：使用真实 Oria node + MockLLM/Fixture Tool + 内存 saver 跑完整图，断言最终 `WorkflowState`、结构化事件、预算计数、工具 observation 和无外部副作用；这只能证明控制流，不证明进程恢复或生产 saver。
3. **Graph-Resume（HITL/外部事件中断与恢复）**：以固定 tenant-qualified thread config 分别运行至审批 `interrupt` 和 `external_wait`，断言审批/等待 payload 与 checkpoint 已存在。审批用锁定版本的 `Command(resume=<decision>)` 恢复；外部事件必须先经过可注入的 event inbox/鉴权/去重 adapter，再以受控 resume payload 恢复，客户端不能直接指定 checkpoint。分别验证 approve/reject/expired/tampered、重复/乱序/错误版本事件，以及恢复前节点可能重放而副作用不重复。真正跨进程恢复另用 `AsyncSqliteSaver/AsyncPostgresSaver` 的 CT/REC，不能用内存 saver 结果替代。

并行图另以 fake branch 同时写入不同 key、同 key 同值和同 key 异值，断言 reducer 前两者确定性合并、异值抛 `StateConflictError` 且不接受冲突 super-step。上述用例分别沿用 `unit / contract / recovery` marker，不另造无法被目标命令收集的 marker。

### 1.4 状态与门禁口径

- **任务完成**：目标产物存在，任务对应的静态检查/测试实际通过，证据已保存；只有代码存在但测试失败时仍是“进行中”。
- **Core Gate**：不依赖付费 Key 或企业网络的必需项通过，是后续版本的代码基线；V0.1–V0.7 默认以 F/C 为 Core，V0.8 最终生产证明额外包含文中明确列出的本地 E-like 容器演练，但仍不把公开模型 Key 或企业 Adapter 凭证纳入 Core。里程碑 ID 固定写作 `V0.x-Core`，可作为后续任务的 `Depends on`。
- **验证卡**：按 provider/backend/environment 独立记录 L/E-like/E。缺环境写 `blocked` 并列 `blocked_by`，不能写 `passed` 或静默 skip。验证任务产出 `passed/failed/blocked` 报告后可关闭本次 run，但只有 `passed` 满足“版本全部通过”；之后条件齐备时以新 run 重验。
- **版本全部通过**：Core Gate 与本版本明确标为必需的验证卡均通过；可选厂商 Adapter 保持独立状态，不阻塞主干实施。

`Depends on` 只表示代码、schema、数据集或契约产物的先后关系，不使用“某次外部验证必须 passed”作为后续实施前置。需要沿用历史实测结果时，在当前验证报告的 `evidence_refs` 引用，不把它伪装成构建依赖。

## 二、每个任务开始前的检查

执行方在写代码前完成以下清单，并在任务记录中留下结论：

- [ ] 阅读 `Oria架构设计.md` 与本文对应版本章节。
- [ ] 确认任务 ID、所属版本、依赖任务和退出门禁。
- [ ] 检查工作区状态，识别并保留无关用户改动。
- [ ] 运行上一版本基线命令；若失败，先记录失败，不在未知基线上继续叠加。
- [ ] 明确本任务使用 F/C/L/E-like/E 哪个验证等级，准备所需 fixture、模型 Key 或容器。
- [ ] 检查数据来源、脱敏方式、许可证和模型 revision；禁止把公司真实客户/商家数据直接放入仓库。
- [ ] 写出可观察的预期行为、失败行为和证据位置，禁止只写“功能正常”。
- [ ] 涉及真实外部写入时确认测试账号、目标、幂等键和清理/对账方案。
- [ ] 若设计与架构冲突，先新增/更新 ADR，再实现。

任务结束时必须：

- [ ] 运行相关 `ruff + mypy + pytest`，只依据实际输出声明通过。
- [ ] 保存脱敏验证报告，列出命令、环境、数据版本、实际结果和未验证项。
- [ ] 对 Live 测试保存 provider request ID、模型名、Token/成本和时间；不得保存密钥或隐式思维链。
- [ ] 更新本文阶段状态；失败项保留为失败/阻塞，不能用 Mock 结果替换。

建议证据路径：`reports/verification/<version>/<run_id>/summary.md`；机器原始输出可放在 gitignored 的 `.artifacts/verification/`。报告至少包含：

```yaml
run_id: "实际生成"
version: "V0.x"
task_id: "V0.x-Txx"
depends_on: []
verification_level: "F | C | L | E-like | E"
commit: "实际 commit；仓库初始化前写 unavailable"
executed_at: "ISO-8601"
environment: "OS / Python / dependency lock hash"
provider_model: "无则写 null"
config_fingerprint: "脱敏后的 ResolvedRuntimeConfig 指纹"
dataset_version: "实际版本"
eval_fingerprint: "无 Eval 则写 null；否则为 dataset/prompt/model/tool schema/gate/lock 的组合 hash"
commands: []
artifacts: []
evidence_refs: []
assertions: []
result: "passed | failed | blocked"
blocked_by: []
known_limits: []
```

### 2.1 目标命令契约

以下命令已由 `V0.1-T01` 建立为执行契约；当前仓库已有 `pyproject.toml`、`uv.lock` 和 Python 3.11 开发环境。后续任务不得另造平行入口；命令是否通过仍必须按每次任务的真实输出记录。

| 目的 | 目标命令 | 适用范围 |
| --- | --- | --- |
| 安装核心与开发依赖 | `uv sync --locked --group dev` | demo/CI |
| 加载本地 BGE | `uv sync --locked --group dev --extra standard` | Community standard |
| 格式检查 | `uv run ruff format --check .` | 每个任务 |
| Lint | `uv run ruff check .` | 每个任务 |
| 类型检查 | `uv run mypy src/oria` | 每个任务 |
| 快速 PR 测试 | `uv run pytest -m "unit or contract"` | 无网络、无容器 |
| 社区完整测试 | `uv run pytest -m "not live and not enterprise and not performance"` | F/C；临时目录隔离 |
| Live Provider | `ORIA_RUN_LIVE=1 ORIA_LIVE_TARGETS=deepseek uv run pytest -m live` | 显式选中的 Provider Key/成本预算 |
| 本地企业栈 | `ORIA_RUN_ENTERPRISE=1 ORIA_ENTERPRISE_TARGETS=postgres uv run pytest -m enterprise` | 显式选中的 E-like 容器组件 |
| 零配置 Demo | `uv run oria demo` | `community+demo` |
| 构建发行包 | `uv build` | wheel/sdist 打包检查 |

若一条命令需要尚未引入的依赖，对应任务先更新 `pyproject.toml + uv.lock`，再执行；禁止不更新锁文件直接在个人环境安装。各命令独立运行并分别保存输出，不能因后一个命令通过而覆盖前一个失败。

`live`/`enterprise` 测试 fixture 必须检查显式运行开关、非空 target 列表、凭证和组件健康状态。`ORIA_LIVE_TARGETS` 使用 provider profile ID，`ORIA_ENTERPRISE_TARGETS` 使用 `postgres,milvus,redis,otel,mcp-http` 等稳定 ID；一次 run 只产生选中 target 的卡片。默认 CI 不收集这些测试；显式选中的 target 若条件缺失，命令应以 non-zero 结束并生成 `blocked` 报告，不能全部 `skip` 后返回成功。未选中 target 是“本次未运行”，不得被写成 passed/blocked；target 列表为空或包含未知 ID 时在测试开始前退出码 2。每个 optional extra 至少有一个独立 CI/import smoke，不能只测试“安装全部 extras”的环境。

`V0.1-T01` 已完成：项目使用锁定的 `uv 0.12.6 + Python 3.11`、`uv.lock` 和仓库本地 `.venv`，并已确认 Oria 根目录为独立 Git 工作树。后续只按锁文件同步，不手工沿用系统或上级目录环境中的包版本；同步失败时先保存错误并确认环境归属，不得直接删除整个 `.venv`。受限环境运行 `uv` 时应把缓存指向仓库内已 gitignore 的 `.artifacts/`，不得写开发者 Home。

### 2.2 CLI 演进契约

子命令在对应任务落地，落地后保持向后兼容；脚本和 README 只调用这些入口，不直接调用内部 Python 函数。

| 首次版本 | 稳定入口 | 用途 |
| --- | --- | --- |
| V0.1 | `oria config doctor`、`oria data init`、`oria demo` | 配置诊断、幂等初始化、MVP 演示 |
| V0.2 | `oria eval run --suite rag` | 固定数据集 RAG/Provider Eval |
| V0.3 | `oria workflow start`、`oria workflow resume`、`oria approval list`、`oria approval decide` | 本地 HITL 与崩溃恢复验证 |
| V0.4 | `oria eval run --suite attribution` | 场景 B 冻结集评测 |
| V0.5 | `oria eval compare --candidate single,multi`、`oria memory list`、`oria memory delete`、`oria memory export` | 公平对照与 Memory 生命周期 |
| V0.6 | `oria db upgrade --target platform\|business\|all`、`oria api serve`、`oria worker run` | 双 revision migration、API 与独立 worker |
| V0.7 | `oria mcp serve`、`oria mcp doctor`、`oria plugins list` | MCP capability 与插件状态 |

CLI 输出默认面向人类且同时支持 `--output json`；自动化测试只断言 JSON schema/退出码，不匹配易变化的彩色文本。成功为退出码 0；输入/配置错误为 2；鉴权拒绝为 3；外部暂时故障为 4；内部错误为 1。不得把异常 traceback 默认展示给普通用户，`--debug` 仍须脱敏。

### 2.3 CI 与 Eval 集成规格

`V0.1-T01` 建立 `.github/workflows/ci.yml`，后续版本只扩展同一工作流。PR required checks 固定拆成独立 job，避免某类结果被其他 job 掩盖：

| Job | 触发/网络 | 内容 | 门禁 |
| --- | --- | --- | --- |
| `quality` | PR/push；禁外网 | ruff format、ruff lint、mypy | 任一失败即阻断 PR |
| `test-core` | PR/push；禁外网 | unit、contract、非外部 integration；收集当前版本已落地的 Graph-UT/E2E-F/HITL | 任一失败即阻断 PR |
| `package` | PR/push；禁外网 | wheel/sdist 构建、安装后 resources/migration/CLI smoke | 任一失败即阻断 PR |
| `extras-smoke` | PR/push；仅依赖安装可联网，测试期禁外网 | `standard/eval/postgres/redis/milvus/otel` 各自独立 matrix import/smoke | 已引入的 extra 任一失败即阻断 PR |
| `eval-golden` | 注册首个 suite 后的 PR/push；禁外网 | MockLLM 录放 + FixtureEmbedder + 冻结 dataset/baseline | 回归门禁失败即阻断 PR |

PR 的 `eval-golden` **不得调用真实模型**。数据、阈值和基线分别版本化在 `eval/datasets/`、`eval/config/gates.yaml`、`eval/baselines/<suite>/<dataset_version>.json`；报告写 `.artifacts/eval/` 并作为 CI artifact 上传，脱敏摘要进入 `reports/verification/`。确定性 suite 的门禁规则固定为：schema、越权工具/跨租户、引用可回查及所有 `critical=true` case 必须 100% 通过；任务成功率、工具准确率、groundedness 等已登记指标均不得低于同 dataset version 的 committed baseline（`allowed_regression=0`）。成本和墙钟时延在 Mock PR job 只报告，不作质量代理。

首次 baseline 只能在人工审阅 case、数据 manifest 和 harness 均通过后创建。修改 dataset 必须升 `dataset_version`；修改 baseline/gate 的 PR 必须带 `eval-baseline-update` label、旧/新逐例 diff 和脱敏证据，并由 `CODEOWNERS` 中的 eval reviewer 批准。普通功能 PR 若同时静默放宽阈值或覆盖基线，`eval-golden` 直接失败。真实模型有随机性，不能通过反复重跑挑最好一次来更新确定性 baseline。

真实 Provider eval 单独放在 `.github/workflows/eval-nightly.yml`，以 `schedule: 0 18 * * *`（北京时间次日 02:00）和 `workflow_dispatch` 触发，不在 fork PR 读取 secrets。目标 provider/model/dataset、重复次数及 `max_cases/max_input_tokens/max_output_tokens/max_cost_usd/max_wall_seconds` 全部来自版本化 `eval/config/nightly.yaml`；美元成本使用其中引用的 `eval/config/pricing/<pricing_snapshot_id>.yaml` 计算，该快照必须含来源、`verified_at/valid_until` 和该 model 的输入/输出/缓存/推理 token 单价。任一预算缺失/非正数、价格缺项/过期、target 未知或凭证缺失，必须在发请求前 non-zero 结束并生成 `blocked` 卡片。Harness 在每次请求前预留预算、每次响应后按实际 usage 扣减，任何上限命中即停止且不对不完整样本计算“通过”。

Nightly 不倒推阻断已合并的单个 PR：它在回归时创建/更新告警和失败报告；`release.yml` 必须要求最近 7 天内存在 passed 的必需 Live 卡，且报告 commit 是发布目标的祖先、报告 `eval_fingerprint` 与发布时重算值完全一致。真实模型质量阈值只有在冻结配置完成重复采样、人工校准并提交 `gates.yaml` 后才成为 release gate；此前结果标为 observational，不能写“质量通过”。Live/Enterprise/性能仍使用 §2.1 的显式 target 选择和独立状态卡，不能合并进 `eval-golden` 冒充免费、确定性的 PR 结果。

## 三、版本总览与状态

| 版本 | 目标 | 当前状态 | 必须包含的真实验证 |
| --- | --- | --- | --- |
| V0.1 | 场景 A 只读提案 MVP | T01–T10 已完成；Core 与必需 Live 卡均通过 | 本地 BGE/Chroma + SQLite；真实 DeepSeek smoke；零业务副作用 |
| V0.2 | Provider/RAG 完整化 | T01–T06 已完成；Core、Nightly 与 DeepSeek 必需 Live 卡均通过 | 真实 BGE 对照评测、tenant/document read ACL；可用 Provider 的 Live adapter 卡片 |
| V0.3 | 场景 A 完整 Workflow | 进行中；T01 已完成，T02–T09 未开始 | SQLite 全链路业务状态、动态确认链、双高风险审批、选品异步恢复、外部副作用幂等/对账 |
| V0.4 | 场景 B 动态归因 Agent | 未开始 | 真实模型对本地分析数据的未知路径归因 |
| V0.5 | 多智能体、上下文和记忆 | 未开始 | 单/多 Agent 同集对照；跨会话记忆生命周期 |
| V0.6 | API 与 Durable Job | 未开始 | SQLite 单 worker社区链路；PostgreSQL 双 worker、杀进程恢复、SSE 与真实 HTTP webhook |
| V0.7 | MCP、插件和扩展后端 | 未开始 | 独立 MCP 进程、临时插件包、真实 Redis |
| V0.8 | 生产证明与旗舰演示 | 未开始 | PostgreSQL/Milvus/OTel 容器迁移、压力/安全/恢复演练 |

## 四、V0.1 MVP：场景 A 只读提案版

### 4.1 目标与永久边界

用户通过 Agent CLI 输入：“基于现行规则为华东餐饮暑期活动筛选 10 家合适商家，给出活动与优惠券批次草案预览、理由和规则引用。”V0.1 使用永久 `research_agent` 有界子图；飞书/钉钉入口先保留统一 ingress/identity 契约与 Mock，真实消息接入不作为 MVP 前置：

```text
model ──business tool_calls──> tools（仅 search_campaign_rules/query_merchants）
  ▲                              │
  └──────── observations ────────┘
  └── structured/plain final ──> validate（软排序草案 + 本地可信规则/引用组装）──pass──> result
                                      │
                                      └──repairable once──> model[finalization-only]
                                                               │（不暴露业务工具）
                                                               └──structured──> validate

policy/contract violation ──> failed
budget/limit/no_progress ────> failed
side_effect_unknown（后续写工具）──> waiting/reconciliation
```

循环必须有最大步数、Token/成本预算、无进展检测和工具 allowlist。`search_campaign_rules` 输出六类规则字段、版本和逐字段引用；`query_merchants` 只接受已校验的 `rule_snapshot_id`，由确定性 EligibilityPolicy 完成类目、城市、黑白名单、报名系统和销售组织等硬条件过滤。LLM 只能在返回候选集内做软条件排序并生成只读 `CampaignProposal`，不得改变硬资格或持久化 Campaign/CouponBatch。后续 V0.3 把该子图作为“规则快照与招商提案”节点，在其后追加活动/券批次草案、运营审核与招商投放、双来源报名/圈品、确认链、券关联、招后选品、C 端投放和通知；V0.4 场景 B 复用同一 model/tools/validate 原语。Provider、Retriever、Tool、State 和 CLI 不另起第二套实现，也不使用已弃用的 `langgraph.prebuilt.create_react_agent`。

### 4.2 实施任务

| ID | Depends on | 任务与产物 | 完成验证 |
| --- | --- | --- | --- |
| V0.1-T01 | — | 确认/初始化 Oria 根 Git；预检并在缺失时安装、记录 `uv` 版本；建立 `.python-version`、`pyproject.toml/uv.lock`、src layout、Typer `oria` console script、`.gitignore`、pytest markers、Live/Enterprise target 选择预检及 §2.3 的 `quality/test-core/package/extras-smoke` CI 骨架；锁定 dev group，并在命令真实可用后同步更新 `AGENTS.md` | `uv --version` 有记录；Python 3.11 锁定安装；仓库根正确；四个 CI job/本地等价命令与 import/CLI smoke；unit/contract 空基线可运行；空/未知 target 退出 2 |
| V0.1-T02 | V0.1-T01 | 建立 Pydantic 值类型、Protocol（含 `InboundRequest/IngressContext/InboundMessage`）、进程级 RuntimeServices/每次执行 Context、actor/executor、本地 PolicyEngine、Domain Service seam、配置模型、只读 `ResolvedRuntimeConfig`、`config doctor` 与 `build_runtime()`/AsyncExitStack 骨架；Runtime ready 后封存 teardown 注册；固定数据目录与 `local-community/local-operator` 社区主体规则 | CFG/Policy/生命周期/入站类型 CT；raw body 只用验签且不持久化；并发 Context 不串 actor/tenant/run；运行期节点不能追加进程 teardown；tenant/roles 不可由自由 CLI 参数冒充；无写 Home/import-time client；fingerprint 脱敏 |
| V0.1-T03 | V0.1-T02 | 建立 wheel 内脱敏招商规则、商家 demo resources 与两条 migration；实现 Merchant model/Repository/Service、EligibilityPolicy 及 business 首个 revision，建立最小 `documents/document_versions/ingestion_runs` platform revision；`data init` 经同一 runner 升级两库并调官方 saver setup | 已安装 wheel 中两条 migration/resource UT；六类规则/商家数据 manifest；EligibilityPolicy/Repository CT；两个空库升级、重复初始化 IT |
| V0.1-T04 | V0.1-T02 | 实现 MockLLMProvider、通用 OpenAICompatProvider 的 DeepSeek Responses-dialect profile、Fixture/BGE Embedder、统一错误/stream/capability；实现 ResponseSchema、profile 级 native/synthetic/unsupported 策略与本地严格校验，不建临时 DeepSeek 专用类 | Provider/Embedder/结构化输出 CT；`/responses text.format` 映射与流事件；非法/混合输出拒绝；缺 Key fail closed；Live 先可 blocked |
| V0.1-T05 | V0.1-T03,V0.1-T04 | 实现 ObjectStore 原文 + platform catalog + Chroma 投影的 ingest/Retriever、document/chunk/version citation 与删除/重建；建立 `CampaignRuleSnapshot` 提取/校验器及 tenant+snapshot_hash 派生缓存/Checkpoint 引用，覆盖基础信息、招商范围、报名规则、优惠档位、确认规则和商家端素材；数据仅写 `data_dir` | Retriever CT/IT；六类字段逐项有来源；缺失/冲突/失效引用拒绝；snapshot ID 不可跨 tenant/篡改且可重算；catalog + content hash 可重建索引；固定问题集记录 Recall@K |
| V0.1-T06 | V0.1-T03,V0.1-T05 | 实现 `search_campaign_rules`、`query_merchants`、ToolRegistry、参数/结果 schema version、allowlist/trust metadata；黑白名单等受限字段只返回必要判定 | Tool/EligibilityPolicy UT/CT；硬规则资格结果确定且可解释；参数、模型可见结果 schema 与引用可回查；LLM 看不到排除集敏感原文 |
| V0.1-T07 | V0.1-T04,V0.1-T06 | 实现 version 必填的 PromptManager、固定 `merchant_selection/v1`、`CampaignProposal` schema、官方 AsyncSqliteSaver adapter 与永久 `research_agent` StateGraph；逐项实现架构 §3.3.1 的路由、observation、大结果、失败、无进展、预算与 repair 语义；建立至少 30 条实际人工审阅的 scenario_a golden、baseline 和独立 `eval-golden` job；Context 经 LangGraph runtime context 逐次传入 | Prompt/Agent/Checkpoint/Context 隔离 CT；Graph-UT/E2E-F；规则字段/硬资格/引用 golden gate；输出只含候选 ID 子集且无写工具；异常终止不死循环，Graph closure 不捕获主体 |
| V0.1-T08 | V0.1-T07 | 完成唯一 `build_runtime()` 装配与 `runtime.new_context()`、`oria demo`、自动初始化、Console JSON/correlation、CampaignProposal schema validator 和验证报告写入 | 初始化失败完整 unwind；同 runtime 两个 run 不串 metadata；源码与已安装 wheel 均在全新临时目录离线运行、重复执行且 business DB 无 Campaign/CouponBatch 写入 |
| V0.1-T09 | V0.1-T08 | README、初版威胁模型、证据模板与 S1 Core report | 文档命令可复制；Core 证据完整且无夸大声明 |
| V0.1-T10 | V0.1-T09 | 执行 S2 DeepSeek+BGE 必需 Live 验证卡 | Live report passed/failed/blocked；保存 model/revision/request ID |

Core 关键路径：`V0.1-T01 → V0.1-T02 → (V0.1-T03 ∥ V0.1-T04) → V0.1-T05 → V0.1-T06 → V0.1-T07 → V0.1-T08 → V0.1-T09`；Live 卡为 `V0.1-T10`。只有 T03/T04 可并行；T05 需要真实 Embedder seam，T07 必须复用正式 Tool/Provider 契约，禁止为赶 Demo 绕过。

### 4.3 真实验证场景

**V0.1-S1：全新目录零配置社区 Demo（F）**

1. 先构建 wheel，再在无源码路径、无 Oria 配置、无云服务凭证的隔离临时环境安装并运行 `oria demo --output json`；源码态另运行 `uv run oria demo --output json`，两者使用同一入口和 schema。
2. 程序自动初始化 fixture、SQLite 和 Chroma，关闭 Chroma 匿名遥测，不访问 Supabase/Redis/Milvus/OTLP/真实 IM。
3. 实际输出必须包含 `search_campaign_rules` 与 `query_merchants` 两种工具事件、六类规则摘要及逐字段有效引用、10 个存在于 fixture 且通过 EligibilityPolicy 的商家 ID、活动/券批次草案预览、推荐理由和 `unresolved_items`；不强制模型采用固定调用顺序。
4. 输出中的推荐 ID 必须是硬资格候选集的子集；删除或冲突规则应明确 abstain，不能由模型补默认值。
5. 第二次运行不得重复破坏初始化数据，且前后 business DB 都不得出现 Campaign/CouponBatch 等副作用记录；删除本地数据后可以重新构建。

**V0.1-S2：真实 DeepSeek + 本地 BGE（C + L）**

1. 设置 `runtime_profile=standard` 和真实 DeepSeek Key，使用锁定 revision 且 `trust_remote_code=false` 的本地 BGE、Chroma、SQLite；报告首次模型下载与后续离线运行的区别。
2. 运行与 S1 相同问题，不固定 LLM 输出文本。
3. 校验实际发生 DeepSeek Responses-dialect 请求，保存 endpoint dialect/model/request ID/usage；两个工具的参数通过 schema；最终 `CampaignProposal` 通过本地 ResponseSchema；推荐商家必须来自 EligibilityPolicy 候选集，规则字段引用必须指向实际召回 chunk，优惠金额/折扣/日期预览必须通过确定性校验。
4. 若模型未调用必要工具、放宽硬规则、伪造商家/引用、擅自补全冲突规则或产生任何业务写入，记录为失败并修复 prompt/graph，不得人工改输出后宣称通过。

### 4.4 测试用例

| ID | 类型 | 用例与断言 |
| --- | --- | --- |
| V01-CFG-01 | CT | `community+demo` 无 Key 成功启动且选择 Mock/Fixture/SQLite/Chroma |
| V01-CFG-02 | CT | `community+standard` 选择 DeepSeek 但无 Key 时 fail closed |
| V01-CFG-03 | SEC | `production+demo`、production Mock 或 FixtureEmbedder 被拒绝 |
| V01-CFG-04 | CT | 多来源配置按 CLI > env > YAML 优先级解析为只读 ResolvedRuntimeConfig；“冲突来源拒绝”特指混合 `${}` 引用、未知 profile 与非 mapping YAML 根等语义错误，不指跨来源同字段覆盖；fingerprint 不含 secret |
| V01-CFG-05 | SEC | production 相对 data_dir 被拒绝；测试 profile 只写注入的临时目录，不访问 Home |
| V01-LIFE-01 | CT | build_runtime 中途失败按逆序关闭已创建资源，Registry/engine/client 无半初始化残留 |
| V01-LIFE-02 | CT | RuntimeServices 对外可用后 `_exit_stack` 已封存；node/tool/request Context 不能注册进程级 teardown，局部临时资源在节点返回前关闭，checkpoint resume 不依赖旧进程清理栈 |
| V01-CTX-01 | CT/SEC | 同一 RuntimeServices 并发创建两个 tenant/run Context；actor/executor/ID/事务上下文互不可见，关闭其中一个 run 不关闭进程资源 |
| V01-PKG-01 | IT | wheel/sdist 包含 prompts/demo resources/manifest/platform+business migrations；隔离环境安装后可升级两库、初始化 saver 并运行 demo，不依赖源码 cwd/ini |
| V01-PROMPT-01 | UT | PromptManager 用 package resources 加载显式固定版本，缺 version、非正/不存在 version 与缺模板变量均拒绝；新增更高版本不改变旧调用的 golden render |
| V01-LLM-01 | CT | Mock 与 DeepSeek Responses adapter 产生相同内部 ChatResult/StreamEvent 结构；reasoning item 不进入最终 content/普通日志 |
| V01-LLM-02 | CT | ResponseSchema 分别覆盖 native、synthetic-tool、unsupported；DeepSeek Responses 映射为 `text.format=json_schema`，不得误发 Chat `response_format=json_schema`；非法 JSON/schema、保留输出与业务工具混合均拒绝，合成输出永不进入 ToolExecutor |
| V01-RAG-01 | IT | 给定 10 个固定问题，expected chunk 可被召回；记录实际 Recall@K，不预填数值 |
| V01-RAG-02 | IT | citation 的 document/chunk/version 在存储中真实存在 |
| V01-RULE-01 | UT/CT | 六类规则字段逐项带 document/version/chunk 引用；缺字段、来源冲突、非法时间窗/折扣/阶梯和失效引用进入 unresolved/拒绝，不猜默认值 |
| V01-RULE-02 | SEC | 黑白名单与内部销售组织原文不进入 prompt/log；仅暴露最小资格判定与脱敏原因 |
| V01-TOOL-01 | CT | `search_campaign_rules` 的 intent/effective_at 与 `query_merchants` 的 rule_snapshot_id/limit 参数校验；未知字段、非法枚举和篡改 snapshot 拒绝 |
| V01-TOOL-02 | UT | EligibilityPolicy 对类目、城市、黑白名单、报名系统、销售组织执行确定性 AND/优先级规则；`query_merchants` 返回 ID 全部来自 SQLite fixture，limit=10 生效 |
| V01-E2E-01 | E2E-F | 同一输入稳定出现两个只读工具事件、六类规则、有效候选/引用和 CampaignProposal；business DB 无活动/券记录 |
| V01-E2E-02 | E2E-L | DeepSeek 真实请求完成；推荐是硬资格候选子集，工具结果、草案字段和引用交叉校验，无任何业务副作用 |
| V01-AGENT-01 | UT/E2E-F | 连续两个 tools super-step 未产生新 evidence fingerprint 才以 `no_progress` 终止；仅 execution/request/trace/timestamp/retry 等易变字段变化仍算重复，同工具不同参数/新语义观察会清零 streak，checkpoint resume 不重置计数 |
| V01-AGENT-02 | SEC | 模型只能看到两个只读工具；fixture/RAG 内容不能扩大 allowlist，LLM 输出任何被硬规则排除的商家 ID 均由 validate 拒绝且不可 repair 掩盖 |
| V01-AGENT-03 | UT/CT | 并行 batch 任一未知/参数/鉴权失败时整批调用数为 0；只读 retryable 失败只由 ToolExecutor 按策略重试；只读最终失败可转向/abstain，重复失败计无进展；副作用 unknown 进入 waiting 且 Agent 不重试 |
| V01-AGENT-04 | UT/SEC | 每个 tool_call 恰有一个按声明顺序追加的 canonical JSON observation；超 32 KiB 转 ObjectStore 引用，原始异常/secret/未授权字段不进入消息，转存失败不回退整段内联 |
| V01-AGENT-05 | UT | `structured_output → validate`、`tool_calls → tools`、二者为空 → validate；不依赖 finish_reason/文本标记；只允许一次 finalization-only schema repair，repair 不暴露业务工具，硬证据/权限错误不 repair |
| V01-AGENT-06 | UT/E2E-F | model turn、tool-call、Token/成本和 deadline 任一到限均产生确定 termination；将越限的并行 batch 整批不执行，Provider usage 已超限时不执行其建议工具 |
| V01-GRAPH-01 | UT | 注入 spy nodes 编译 research graph，分别断言 tool、final、validation-repair、强制终止路径及未访问节点 |
| V01-GRAPH-02 | E2E-F | 真实 Oria nodes + Mock/Fixture + 内存 saver 跑完整图，最终 state、事件、预算和 observation 可断言；不把该结果写成跨进程恢复通过 |
| V01-CKPT-01 | CT/SEC | 两个 tenant 使用相同 external thread_id 产生不同 storage_thread_id；任一方 list/get/resume 均不可见另一方 checkpoint，API/日志不暴露 storage key |
| V01-OFFLINE-01 | SEC | 阻断外网后 demo profile 仍可跑；任何意外网络连接使测试失败 |
| V01-LOG-01 | SEC | 输出/日志不含 API Key、完整 prompt、隐式思维链和未脱敏数据 |

### 4.5 退出门禁

**Core Gate（允许实施 V0.2）**：

- V0.1-T01–T09 产物完成；目标安装、ruff、mypy、Fixture/Community 测试实际通过。
- scenario_a 数据已完成人工审阅并冻结版本；六类规则字段、逐字段引用、硬资格期望集和 CampaignProposal schema 均进入独立 `eval-golden` required job，且对 committed baseline 无负回归。
- S1 在全新临时目录通过并保存命令和输出；README 明确区分 demo 与真实模型结果，不出现企业级性能声明。
- wheel/sdist 中实际包含 Prompt、脱敏规则/商家 demo data 和 manifest；从已安装 wheel 运行不读取源码 checkout；MVP 全程只读且数据库无 Campaign/CouponBatch/投放记录。

**必需 Live 验证卡（允许声明“真实模型 MVP 已验证”）**：

- V0.1-T10/S2 至少完成一次真实 DeepSeek + BGE run，BGE model/revision/license 与 DeepSeek model/request ID 进入报告。
- 若无 Key 或模型下载条件，卡片写 `blocked`，V0.1 状态为“Core 完成，Live blocked/待验证”；可以继续 V0.2 实施，但不得写“V0.1 全部通过”。

### V0.1 验证状态（2026-08-29）

- 代码与文档状态：`V0.1-T01–T10` 已完成。
- Core Gate：通过；188 项 Core 测试、30/30 Golden、静态检查、wheel/sdist、源码态与隔离已安装 wheel的全新目录双跑均通过。
- Live 卡：通过；Agent 审计修复后真实 `deepseek-v4-flash` Responses + 锁定 BGE 双跑完成，7/7 模型轮次保存 request ID 与 usage，并保存 52 条引用、幂等与业务库完整指纹无变化证据。
- Enterprise 卡：未运行。
- 证据：
  - [`reports/verification/v0.1/20260827T084858+0800/summary.md`](../reports/verification/v0.1/20260827T084858+0800/summary.md)（T01 工程基线）
  - [`reports/verification/v0.1/20260827T112706+0800/summary.md`](../reports/verification/v0.1/20260827T112706+0800/summary.md)（T02 首次报告，F 等级；`result: passed` 判定过早，保留为失败历史）
  - [`reports/verification/v0.1/20260827T133710+0800/summary.md`](../reports/verification/v0.1/20260827T133710+0800/summary.md)（T02 remediation，F 等级；源码门禁通过，wheel 构建因环境缺 `hatchling` 记为 blocked，保留不改）
  - [`reports/verification/v0.1/20260827T134842+0800/summary.md`](../reports/verification/v0.1/20260827T134842+0800/summary.md)（T02 package 门禁补验，F 等级，`result: passed`，commit `2469967`，47 passed）
  - [`reports/verification/v0.1/20260827T143158+0800/summary.md`](../reports/verification/v0.1/20260827T143158+0800/summary.md)（T03 首次报告，F 等级，commit `1b67b43`，82 passed；后续代码审查发现四项未覆盖问题，保留为历史证据）
  - [`reports/verification/v0.1/20260827T214917+0800/summary.md`](../reports/verification/v0.1/20260827T214917+0800/summary.md)（T03 remediation 01，F 等级，`result: passed`，86 passed；修复两个 P1 与两个 P2，当前尚未提交）
  - [`reports/verification/v0.1/20260827T232744+0800/summary.md`](../reports/verification/v0.1/20260827T232744+0800/summary.md)（T04，F 等级，`result: passed`，107 passed；MockTransport/Fake BGE 契约 + 隔离 wheel，未运行真实 DeepSeek/BGE）
  - [`reports/verification/v0.1/20260828T005408+0800/summary.md`](../reports/verification/v0.1/20260828T005408+0800/summary.md)（T04 remediation 01 + T05，F 等级，`result: passed`，138 passed；含 T02–T05 隔离 wheel 门禁）
  - [`reports/verification/v0.1/20260828T082536+0800/summary.md`](../reports/verification/v0.1/20260828T082536+0800/summary.md)（T05 remediation 02 + T06，F 等级，`result: passed`，145 passed；含 T02–T06 隔离 wheel 门禁）
  - [`reports/verification/v0.1/20260828T090353+0800/summary.md`](../reports/verification/v0.1/20260828T090353+0800/summary.md)（T07 代码 F 等级通过，171 passed + 隔离 wheel；30 条 Golden 为 `pending_human_review`，故整体 `result: blocked`）
  - [`reports/verification/v0.1/20260829T001659+0800/summary.md`](../reports/verification/v0.1/20260829T001659+0800/summary.md)（T07 Golden remediation 01；`sa-v1-027` 写工具提示词注入改为整批 fail closed，173 passed；Golden 仍待人工审阅）
  - [`reports/verification/v0.1/20260829T004513+0800/summary.md`](../reports/verification/v0.1/20260829T004513+0800/summary.md)（T07 收口；30/30 Golden、5 项指标全为 1.0，174 passed，构建与已安装 wheel 门禁通过，`result: passed`）
  - [`reports/verification/v0.1/20260829T093711+0800/summary.md`](../reports/verification/v0.1/20260829T093711+0800/summary.md)（T08；源码态与已安装 wheel 在全新目录离线双跑，178 passed，零 Campaign/CouponBatch 副作用，`result: passed`）
  - [`reports/verification/v0.1/20260829T101609+0800/summary.md`](../reports/verification/v0.1/20260829T101609+0800/summary.md)（T09 / V0.1-Core；178 passed、30/30 Golden、源码/wheel S1 双跑、威胁模型与证据模板收口，`result: passed`）
  - [`reports/verification/v0.1/20260829T104059+0800/summary.md`](../reports/verification/v0.1/20260829T104059+0800/summary.md)（T10 DeepSeek+BGE Live 预检；缺 Key/standard 依赖/BGE 缓存，请求前 fail closed，`result: blocked`）
  - [`reports/verification/v0.1/20260829T145723+0800/summary.md`](../reports/verification/v0.1/20260829T145723+0800/summary.md)（T10 DeepSeek+BGE Live；真实 Responses + BGE 双跑、180 passed、Golden 30/30，`result: passed`）
  - [`reports/verification/v0.1/20260829T160420+0800/summary.md`](../reports/verification/v0.1/20260829T160420+0800/summary.md)（V0.1 Agent 审计修复；保留首次 `limit=100` 失败并追加动态 Tool Schema 后真实双跑，188 passed、7/7 request ID/usage、业务库指纹不变，`result: passed`）
- T02 remediation 修复：Runtime ready 后整体不可替换/扩展；跨 seam 容器深度不可变；JsonValue 拒绝非有限浮点且显式导出；reasoning/provider raw 默认公开投影脱敏；配置矩阵、生命周期边界、模块/schema/union/wheel/CI 覆盖收紧。详细缺陷与命令见对应报告。
- T03 交付：类型化 Domain Service 契约（`ctx.domain` 公开成员精确为 `{campaign_rules, merchants}`，无 repository/engine/session 旁路）、确定性 EligibilityPolicy（denylist 优先，对照 ADR-028）、platform/business 两条 migration 与各自独立版本表、wheel 内全合成 demo resources（六类规则字段 + 12 商家 + sha256 manifest + `contains_real_entities: false`）、`oria data init`（双库同一 runner 升级 + 官方 saver setup + 幂等）。
- T03 remediation 01：Domain Service 复核规则与 Repository 记录的 tenant；migration runner 校验列、类型、nullable、主键和复合外键，拒绝 lookalike schema + forged head；应用 SQLite 连接强制 `foreign_keys=ON`；Alembic `SQLAlchemyError` 归一化为脱敏 CLI JSON 错误。新增四条回归，总计 86 passed、15 条 T03 绕道用例，并完成隔离 wheel 验证。
- T04 交付：接受 ADR-001，实现 MockLLMProvider、DeepSeek Responses profile 的 OpenAICompatProvider、Fixture/BGE Embedder、profile 级 native/synthetic/unsupported、本地严格 schema 校验、统一 stream/error/capability 和唯一 `build_runtime()` 装配。remediation 01 对 native/synthetic 流式结构化输出先缓冲再校验，拦截保留工具与业务工具混合，并补齐 Mock 常用严格 schema witness、BGE 单位向量验证与凭证键变体脱敏。真实 DeepSeek/BGE 仍未运行。
- T05 交付：实现 tenant-qualified ObjectStore、platform catalog、按 provider/model/revision/dimension 指纹隔离的 Chroma 投影、ingest/delete/rebuild、tenant/ACL 前后过滤和 ObjectStore 原文回源；`CampaignRuleSnapshot` 覆盖六类规则、商品圈选/招后选品字段、金额/折扣/阶梯校验与列表叶子 citation。Fixture 固定 10 问实测 `Recall@3=10/10`，缺失/冲突/失效/篡改、跨 tenant、受限字段泄露、向量文本污染和 embedding profile 切换均有回归；已新增 T05 隔离 wheel CI 门禁。
- T05 remediation 02：文档删除会遍历同一 Chroma store 中的全部 Oria projection；向量与 ObjectStore 清理成功后才提交 catalog 软删除，失败重试仍可从完整版本记录恢复对象引用。双 profile 残留与向量删除故障注入均有回归。
- T06 交付：实现启动期 allowlist 并封存的 ToolRegistry、`search_campaign_rules` 和 `query_merchants`；工具参数与模型可见结果均使用 versioned strict schema，执行前调用统一 PolicyEngine，成功结果携带 trust/provenance/classification。规则工具只返回六类脱敏规则与模型可见字段 citation；商家工具只接受完整性校验通过的 `rule_snapshot_id`，由 EligibilityPolicy 执行硬过滤，返回候选、数量和无 ID 的排除原因汇总，黑白名单成员与销售组织原文不进入模型结果。
- T07 代码交付：实现显式版本 PromptManager、`merchant_selection/v1`、CampaignProposal 证据交叉校验、永久 `model/tools/validate` StateGraph、整批预检/canonical observation/32 KiB 转存/只读重试/无进展/一次 finalization-only repair/全量预算终止，以及官方 AsyncSqliteSaver 的 tenant-safe 异步适配。Fixture 已调整为 12 条中确定 10 条合格候选。
- T07 Golden remediation 01：`sa-v1-027` 不再把写工具提示词注入降级为可继续生成提案；预期改为 `runtime_failure/policy_or_contract_violation`，`expected_tools=[]`，`persist_campaign` 仍为禁止工具。已更新 dataset sha256 并增加显式契约回归；既有 Agent 整批预检已证明未知写工具不执行任何调用。
- T08 交付：唯一 `build_runtime()` 现在装配官方 tenant-safe SQLite Checkpointer 与永久 `research_agent`；无状态 `DemoMockLLMProvider` 仅依据当前 observation 驱动两个正式只读工具，不持有 run metadata。`oria demo` 自动升级/播种/建索引，每次调用创建独立 Context 并输出带 correlation/run ID 的 JSON 事件与 usage；结果再经 ResponseSchema、可信证据、引用可回查、候选子集与业务零副作用校验后，原子写入 `data_dir/reports-tmp/<run_id>.json`。严格 schema 转换同时修复了 typed dynamic map 被误封闭的问题。
- T09 交付：README 现可直接复制离线 Demo/Golden/构建命令，并明确区分 Core、Live 与 Enterprise；`docs/security/V0.1威胁模型.md` 记录 13 类威胁、当前缓解和剩余风险，包含 `sa-v1-027` 提示词注入与 `sa-v1-028/029/030` 商家资格边界；ADR-015 冻结 Eval 分层门禁，`reports/verification/TEMPLATE.md` 冻结证据字段与结果判定。
- T10 交付：按 `uv.lock` 安装 standard extra，BGE revision 前移到含 safetensors 的不可变提交并完成首次/强制离线真实推理；修复纯工具轮结构化解析、5 秒 Provider 默认超时和权威字段由 LLM 机械回写问题。DeepSeek 只输出候选集内软排序草案，规则、预览和 52 条引用由可信 Tool 结果本地组装；真实双跑保存 6 个成功 request ID，第二次初始化/摄取幂等，业务零副作用。
- V0.1 Agent 审计修复：结构化失败响应携带并累计 request ID/model/usage，缺失或非法 usage fail closed；原始用户请求只保留在 user message，Prompt 与 `CampaignProposalDraft` schema 对齐；`max_candidates` 动态收窄模型可见 Tool Schema并由执行端和最终提案再次校验；Demo 对 Agent 执行前后业务库完整指纹做一致性断言。首次 Live 回归拦截 DeepSeek 的 `limit=100` 调用并保留失败证据，修复 Tool Schema 后真实双跑通过。
- 判定历史：T02 首次报告在 Runtime 组成仍可替换、嵌套值仍可修改、敏感字段仍可默认序列化且 security 未进入 required `test-core` 时写为 `passed`，属于判定过早；T03 首次报告也未覆盖「Repository seam 返回跨 tenant 数据」「伪造完整表名并 stamp head」「SQLite 外键实际未启用」「Alembic 包装异常绕过 CLI 错误边界」。旧报告按 §1.1 第 5 条不改写，均由后续 run 追加纠正。**每个任务的测试清单继续强制区分直接路径与绕道断言**。
- 当前门禁结论：`V0.1-Core` 与 T10 必需 Live 卡均已通过，可作为后续任务依赖。允许声明“V0.1 社区版真实模型 MVP 已验证（DeepSeek + 锁定本地 BGE）”；不得扩展为其他 Provider、企业 Adapter、真实客户数据或生产规模已验证。远端 GitHub Actions 尚未在本次未提交变更上实跑。

## 五、V0.2 Provider 与 RAG 完整化

### 5.1 实施任务

| ID | Depends on | 任务与产物 | 完成验证 |
| --- | --- | --- | --- |
| V0.2-T01 | V0.1-Core | 扩展 OpenAI-compatible 四家 profile、Anthropic 和 Mock 的 capability/error/stream/structured-output 契约；每个 profile 固定 api dialect 并显式声明 native/synthetic/unsupported；Claude 支持时映射 `output_config.format`，旧 profile 才走 synthetic tool；模型来自显式配置或 model-list 探针 | 统一 Provider CT；endpoint dialect × 结构化输出策略矩阵与 Anthropic 双路径；每家状态卡初始化 |
| V0.2-T02 | V0.1-Core,V0.1-T02 | 新增 platform revision（tenant/subject/read policy/audit/outbox）与 Platform AuditService，把 PolicyEngine 扩展为 document read ACL、PolicyDecision→ACLFilter、deny-by-default | platform migration + Policy/Audit CT/SEC；query filter 不可覆盖；拒绝决策脱敏落库 |
| V0.2-T03 | V0.2-T02,V0.1-T05 | 在 V0.1 platform catalog 上增加 owner/ACL/classification 与版本策略，完成 AuthorizedRetriever、更新/删除传播、引用校验及 catalog→vector 重建 | 升级/生命周期/重建 IT；跨 tenant/ACL SEC |
| V0.2-T04 | V0.2-T03 | 加入 BM25、dense fusion、reranker 与可配置检索管线 | 三配置同接口 CT/PERF；无静默 fallback |
| V0.2-T05 | V0.2-T03,V0.2-T04 | 建立不少于 60 条人工审阅 RAG case、开发集/冻结 holdout、数据 manifest、Eval harness 与 `eval run --suite rag`；登记 `eval-golden` baseline/gates，建立带硬预算的 `eval-nightly.yml/nightly.yaml`；用锁定的真实 BGE revision 执行 S1 三管线对照，更新 RAG 威胁模型 | 数据 schema/污染检查；PR gate/baseline-update/nightly blocked 路径 CT；C 级固定 run 产出原始 Recall@K/MRR/引用/延迟结果 |
| V0.2-T06 | V0.2-T01,V0.2-T05 | 执行 DeepSeek 必需卡与其他 Provider 独立 Live 卡，更新 adapter 状态 | request ID/model/usage 报告；缺 Key 明确 blocked |

模型 ID 不得把会退役的示例别名固化为长期默认；开发集可用于调参，冻结 holdout 一旦使用就只能追加新版本，不能原地改答案。

### 5.2 真实验证场景

**V0.2-S1：真实本地 RAG 对照（C）**：同一 BGE revision、同一冻结 holdout 依次运行 dense、dense+BM25、dense+BM25+reranker，保存每种配置的 Recall@K、MRR、引用命中、延迟；只能报告实际差值，并同时报告数据量与置信区间/样本局限。

**V0.2-S2：权限与生命周期（C）**：创建两个 tenant 和不同 ACL 文档；分别查询、更新、删除并重建索引，验证不可跨租户召回，旧 version 不再被引用。

**V0.2-S3：Provider Live 卡片（L）**：对每个有真实 Key 的 adapter 执行文本、流式、工具调用、错误映射 smoke。没有 Key 的 adapter 保持 `live_verified=false`，不能由兼容接口 Mock 替代。

### 5.3 核心测试

| ID | 类型 | 断言 |
| --- | --- | --- |
| V02-PROV-01 | CT | 所有 adapter 通过统一消息、tool-call、stream、usage、错误映射套件 |
| V02-PROV-02 | L | 每个可用 Provider 保存真实 request ID、model 和 usage |
| V02-PROV-03 | CT | 每个 profile 的 api dialect、native/synthetic/unsupported 与 capabilities 一致；Chat/Responses payload 不混用，Anthropic native `output_config.format` 与 synthetic 保留工具双路径、非法/混合提交和本地 schema 校验通过统一套件 |
| V02-EVAL-01 | CT/E2E-F | PR eval 禁止网络且只用 Mock/Fixture；确定性指标/critical case 负回归阻断，普通 PR 静默改 baseline/gate 被拒绝 |
| V02-EVAL-02 | CT | Nightly target/五项预算/凭证缺失时请求数为 0、退出非零并生成 blocked 卡；预算耗尽时不对不完整样本判 passed |
| V02-RAG-01 | IT/PERF | 三种检索配置在同一数据集运行并生成原始评测结果 |
| V02-RAG-02 | SEC | 调用方 query filter 不能移除 tenant/ACL filter |
| V02-RAG-03 | SEC | 含恶意指令的文档只能作为引用数据，不能增加工具权限 |
| V02-RAG-04 | IT | 文档更新/删除传播到 chunk/vector/citation，不返回过期版本 |
| V02-POL-01 | CT/SEC | 无 actor/executor、主体与 Context 不一致或未知 action 默认拒绝；PolicyDecision 生成不可覆盖 ACLFilter，跨 tenant/document ACL 查询无结果且留脱敏审计 |

T01 交付：OpenAI-compatible adapter 现按 profile 显式支持 Responses 与 Chat Completions 两种方言，Anthropic adapter 按 Messages content block 做双向转换；Mock、DeepSeek、Kimi、智谱、OpenAI、Anthropic 共用消息、工具、usage、stream、错误和结构化输出契约。V02-PROV-01/V02-PROV-03 仅使用 `httpx.MockTransport` 固定 fixture 验证，六家 V0.2 状态卡均初始化为 `live_verified=false`；本任务未运行真实网络，不能把 CT 结果声明为 Live 支持。

T02 交付：新增 `platform_0003` 的 `read_policy/audit_events/outbox` 表及升级/回滚契约，新增不可变且默认拒绝的 `ACLFilter` 与 `EventEnvelope`；本地 PolicyEngine 对 document/rule read 生成 tenant + subject/role + classification ACLFilter，Retriever 的 Chroma pre-filter、catalog post-filter 与 citation load 统一消费该决策，调用方过滤不能覆盖策略字段。Platform AuditService 对拒绝决策单独 append 并按字段脱敏，`production + restricted` 写库失败会 fail closed。V02-POL-01 的 Fixture/Community CT/SEC、本地 SQLite migration、完整社区套件与 30 条 Golden 已通过；本任务未运行真实网络、Live、Enterprise 或 Performance，也未实现 T03 的完整文档生命周期增强。

T03 交付（已完成，Community）：新增 `platform_0004` document-version lifecycle revision，把 owner/classification 下沉到不可变的 `(tenant_id, document_id, version)` 并增加 `superseded_at`；新版本成功建立投影后原子地 supersede 旧版本，按版本清理全部 Oria Chroma projection，清理故障可经当前版本幂等摄入重试；删除仍遍历全版本清理 ObjectStore/向量投影，rebuild 仅重建 active version。AuthorizedRetriever 与 citation load 现同时复核 tenant/ACL/current version/content hash/chunk metadata，`Doc` 显式标记为 `untrusted_data`。V02-RAG-02/03/04 与 V0.2-S2 的双 tenant、不同 ACL、查询/更新/删除/重建闭环已用本地 SQLite + Chroma + 合成 Fixture 通过；完整社区套件 `277 passed, 1 deselected`，security `49 passed`，Golden `30/30`，静态检查与隔离 wheel 验证通过。证据：[`reports/verification/v0.2/20260829T214048+0800/summary.md`](../reports/verification/v0.2/20260829T214048+0800/summary.md)。本任务未运行 Live、Enterprise 或 Performance，V0.2 Core 仍等待 T04–T05 及真实 BGE 对照。

T04 交付（已完成，Community）：自建纯 Python BM25 检索投影（标准 tf 饱和 + Robertson/Sparck Jones 正化 IDF + 文档长度归一化，无 numpy/rank-bm25 依赖），与 dense 投影同步 upsert/删除/重建，按 tenant 追踪 readiness 且未构建时显式抛错而非静默降级；新增 `ConfigurableRetriever` 提供 dense / dense+BM25（Reciprocal Rank Fusion）/ dense+BM25+reranker 三配置同一接口，新增 `Reranker` seam 与确定性 `FixtureReranker`，`AuthorizedBM25Retriever` 与 dense 一样复核 tenant/ACL/current version/content hash，检索结果仍标记 `untrusted_data`。runtime 默认装配 dense 管线（保持 demo 行为），bm25/reranker 组件已就位可切换。三配置真实运行、无静默 fallback、BM25 评分/ACL/生命周期契约测试已用本地 SQLite + Chroma + 合成 Fixture 通过；完整社区套件 `282 passed, 1 deselected`，静态检查与隔离 wheel 验证通过。本任务未接入真实 cross-encoder reranker，未运行真实 BGE 三管线 Recall@K/MRR 对照（留 T05），也未设性能阈值（仅原始延迟基线）。

T05 交付（已完成，Fixture/Community）：60 条全合成 RAG v1 已人工批准（42 development / 18 holdout，六类各 10 条，12 条 critical 按两个 split 各 6 条）并冻结 holdout；跨类别、源文档不可回答和直接复述 chunk 标题的问法已修订，空 critical split 与虚假实例标签均 fail closed。Fixture 首次 baseline 与 `rag-gates.yaml` 已提交，`eval_fingerprint` 绑定 dataset/model/pipeline/gate/`uv.lock`，PR `eval-golden` 与定时 Nightly 零请求预检已接入。锁定 `BAAI/bge-small-zh-v1.5@a7ec…e9d` 与 `BAAI/bge-reranker-base@2cfc…a70` 在冻结 60 条上完成离线复跑：dense、hybrid、hybrid+rerank 的 Recall@3/MRR 分别为 `0.9333/0.8389`、`0.9667/0.8778`、`0.9833/0.9222`，引用命中率和 critical pass rate 均为 `1.0`。完整套件 `310 passed, 1 deselected`，静态检查、构建和安装态 wheel 门禁通过。证据：[`reports/verification/v0.2/20260830T152625+0800/summary.md`](../reports/verification/v0.2/20260830T152625+0800/summary.md)。

T06 交付（真实本机 Live，passed）：带五项硬预算的 Provider Nightly 已在冻结 holdout 6 条 critical case × 2 次上完成 `12/12` 请求，模型均为 `deepseek-v4-flash`，总 input/output 为 `2446/465` tokens，按冻结 peak 价格估算成本 `$0.001090232`。独立诊断定位到 DeepSeek V4 默认思考模式拒绝显式 `tool_choice`；适配层仅对 DeepSeek Responses 的显式工具选择发送 `reasoning.effort=none`。修复后二次诊断的官方字符串与 Oria 消息输入均成功调用工具，最终 Provider Live 的文本、语义流式、工具调用和 401 错误映射全部通过，卡片更新为 `live_verified=true`。失败历史证据保留于 [`20260830T163443+0800`](../reports/verification/v0.2/20260830T163443+0800/summary.md)，修复后通过证据：[`reports/verification/v0.2/20260830T171211+0800/summary.md`](../reports/verification/v0.2/20260830T171211+0800/summary.md)。

Core Gate：V0.2-T01–T06、真实 BGE 对照、ACL/删除、安全测试、真实 DeepSeek Nightly 与必需 Live 卡均已通过，V0.2 完成并允许实施 V0.3。Anthropic 及其他 Provider 使用独立可选 adapter card，不阻塞 V0.3，但未 Live 验证的厂商不得进入“已验证支持”列表。

## 六、V0.3 场景 A 完整 Workflow

### 6.1 实施任务

| ID | Depends on | 任务与产物 | 完成验证 |
| --- | --- | --- | --- |
| V0.3-T01 | V0.2-Core,V0.1-T03 | 在现有 Alembic 基线上追加 ProductSnapshot/CampaignRuleSnapshot/Campaign/CouponBatch/LaunchSagaState/RecruitmentPublication/Enrollment/EnrollmentItem/EnrollmentCouponLink/ConfirmationTask/AssortmentSubmission+SelectionDecision/ConsumerPlacement/MerchantNotification、`merchant\|auto\|hybrid` 报名模式、状态机和 Repository revision | 领域 UT；tenant 复合约束；ProductSnapshot/规则版本引用可复核；V0.1→V0.3 升级、空库全量升级/回滚 IT |
| V0.3-T02 | V0.2-T02,V0.3-T01 | 追加 Platform approval/external_waits/integration_event_inbox revision；实现写操作 RBAC/职责分离、ToolPolicy `approval_mode`、`launch_approval`/`consumer_publish_approval`、policy version 与恢复时重新鉴权；实现 `IntegrationEventEnvelope` 事件 union、wait/resource/version/checkpoint 绑定和 inbox 以 `(tenant_id,adapter_id,source_event_id)` 去重；在 Business 侧实现规则驱动的 BusinessConfirmationPolicy/timeout action | platform/business migration；deny/self-approval/跨 tenant SEC；重复/乱序/未知类型/错误版本事件不恢复；动态零/多级确认链与超时 UT |
| V0.3-T03 | V0.3-T01,V0.3-T02 | 在 Business DB 实现 execution ledger、canonical args/plan hash、domain/audit/outbox、receipt、unknown/reconciliation 与同事务边界；审批状态与 Platform audit/outbox 同事务，跨库只做幂等投影/对账 | 两库分别 rollback/重复执行/对账 IT/REC；禁止跨库双写或伪装全链路原子 |
| V0.3-T04 | V0.3-T03,V0.1-T07 | 实现 `persist_campaign_draft`、`materialize_coupon_batch`、`publish_recruitment` 领域 Service/Tool；`launch_approval` 绑定含规则/草案/券/范围/素材/子步骤/补偿策略 hash 的不可变 LaunchPlan，批准后以 checkpointed saga 执行两个外部副作用 | 草案无外部副作用；规则/金额/日期/素材校验；审批篡改拒绝；券已物化但投放失败时不伪回滚，仅允许已验证的幂等补偿，否则进入对账 CT/IT/REC |
| V0.3-T05 | V0.3-T03,V0.3-T04 | 实现商品库 Adapter/Repository、`query_eligible_products` 与 ProductEligibilityPolicy，强制绑定 `product_circle_policy_ref/version`；按 `merchant\|auto\|hybrid` 实现自主报名事件/窗口关闭与系统自动圈品分支、`upsert_enrollment_items`、确认链、`link_coupon_batch`；统一业务唯一键汇聚双来源 | 商品快照/分页一致性、三模式分支/join、重复/迟到 webhook、关窗后默认拒绝或新版本、来源合并、价格/类目/关键词/资格、确认主体/超时、无悬空券关联 UT/CT/IT |
| V0.3-T06 | V0.3-T03,V0.3-T05 | 实现 `submit_assortment` 与选品 Adapter（Mock + 契约），强制绑定与自动圈品分离的 `assortment_policy_ref/version`；通过标准化 selection event union 接收 webhook/轮询结果；实现 `publish_consumer_placement`、`send_merchant_notification`；`consumer_publish_approval` 绑定 selection/link/placement 版本 | 选品提交默认中风险自动、不可撤销/广范围策略升级 HITL；只投放 selected 且券关联有效商品；结果变化使审批失效；unknown 对账；通知重试/死信不回滚投放 CT/IT/REC |
| V0.3-T07 | V0.3-T02,V0.3-T04,V0.3-T05,V0.3-T06 | 在原 Graph 追加完整预定流程：草案 → launch HITL/saga → 按 `merchant\|auto\|hybrid` 路由自主报名/自动圈品并在关窗后 join → 动态确认/券关联 → 选品提交/event wait → consumer publish HITL → C 端投放 → 商家通知；实现五种 builder、确定性 reducer 及 workflow/approval CLI | Graph-UT/HITL/interrupt/event-resume CT；reducer 冲突 UT；10 步 E2E-F；CLI 可注入 Mock 报名/关窗/选品事件 |
| V0.3-T08 | V0.3-T07 | 执行 S1–S6 Fixture/Community 故障注入，更新威胁模型与 Core 证据 | C/REC/SEC 报告；重复计数、状态机、数据库与回执断言 |
| V0.3-T09 | V0.3-T08,V0.2-T01 | 执行真实 DeepSeek 草案/软排序必需 Live 卡；如有 V0.1 Live 证据则引用但不作构建前置 | L report passed/failed/blocked；LLM 不改硬资格且无直接写路径 |

V0.3 Community Core 以 CLI + Mock Feishu/DingTalk ingress、Mock 券/商家侧/选品/C 端/通知 Adapter 跑完整业务语义；真实 IM 和企业系统逐 Adapter 保存 E 卡，缺环境不得冒充已接入。禁止用启动时 `create_all` 代替 migration，也禁止在 Graph node 内直接拼 SQL；LLM 只在 EligibilityPolicy 候选集内做软排序并生成草案，所有写入经领域 Service。

T01 交付（已完成，Fixture/Community）：新增 14 个不可变招商业务值模型、Campaign/CouponBatch 集中式状态机、tenant 复合唯一键与 `merchant|auto|hybrid` 双来源幂等汇聚；`business_0002` 在 merchants 基线上新增 14 张表，所有外键含 tenant，并支持空库/V0.1 升级及回滚。14 个具名 Repository Protocol 与 SQLite 实现只接受业务 ID/复合唯一键，写路径强制 tenant 与乐观锁，Campaign/CouponBatch 状态不能经通用 upsert 裸改。完整社区套件 `359 passed, 1 deselected`，静态检查、构建与隔离安装 wheel 验证通过。证据：[`reports/verification/v0.3/20260830T204753+0800/summary.md`](../reports/verification/v0.3/20260830T204753+0800/summary.md)。本任务未实现 T02–T09 的审批、事件恢复、业务 Service/Tool/Graph、完整 S1–S6 或企业 Adapter，也未运行 Live/Enterprise/Performance。

T02 交付（已完成，Fixture/Community）：新增严格的 `Approval`/`IntegrationEventEnvelope`/`ExternalWait` 值类型、Pydantic schema 驱动的 canonical args hash、冻结规则驱动的零到多级 `BusinessConfirmationPolicy`，以及带职责分离、双闸门隔离、policy/checkpoint/args 绑定和恢复时重新鉴权的 `ApprovalService`。`LocalPolicyEngine` 已扩展为可信主体目录与 action-role 显式写 RBAC；`platform_0005` 新增 approvals/external_waits/integration_event_inbox，并由 SQLite Repository 以 `(tenant_id, adapter_id, source_event_id)` 去重，只保存脱敏 payload 与原 payload hash。重复、未知、未授权、类型/资源不匹配、stale/out-of-order、错误 schema version 和过期 wait 均不可进入恢复资格；Graph CAS 恢复接线仍归 T07。完整社区套件 `405 passed, 1 deselected`，独立安全套件 `70 passed, 336 deselected`，静态检查、构建与隔离安装 wheel 验证通过。证据：[`reports/verification/v0.3/20260830T212314+0800/summary.md`](../reports/verification/v0.3/20260830T212314+0800/summary.md)。本任务未实现 T03–T09 的 execution ledger、业务 Tool/Saga/Graph、完整 S1–S6 或企业 Adapter，也未运行 Live/Enterprise/Performance。

T03 交付（已完成，Fixture/Community）：新增严格的 execution ledger/domain event/Business audit/outbox/receipt 与 LaunchPlan 值类型，plan hash 复用 ADR-024 canonical 规则并绑定全部声明子步骤；`business_0003` 新增 `tool_executions`、`domain_events`、Business `audit_events/outbox`，runner 对 Platform/Business 同名表分别执行完整 schema 校验。`ExecutionLedger` 已实现 reserve-first、稳定业务 ID+args hash 幂等键、重试读历史、unknown 禁止盲投与 reconcile 收敛；成功业务写、ledger、domain/audit/outbox 在同一 Business 事务，approval 状态与 Platform audit/outbox 在同一 Platform 事务，两库不跨库双写。完整社区套件 `420 passed, 1 deselected`，独立安全套件 `70 passed, 351 deselected`，Ruff format/Lint 与 mypy 通过。证据：[`reports/verification/v0.3/20260830T220602+0800/summary.md`](../reports/verification/v0.3/20260830T220602+0800/summary.md)。本任务未实现 T04–T09 的业务 Tool/Saga/Graph、完整 S1–S6 或企业 Adapter，也未运行 Live/Enterprise/Performance。

T04 交付（已完成，Fixture/Community）：实现了无外部副作用的 `persist_campaign_draft`、绑定规则/草案/券/范围/素材/固定双子步骤/补偿策略的不可变 `LaunchPlan`、共用 `launch_approval` 的 `materialize_coupon_batch`/`publish_recruitment` 独立账本工具，以及可从中间 ledger 历史恢复的 checkpointed saga。`business_0004` 将 saga CHECK 收紧为固定主链和三个失败终态；投放失败/unknown 不伪回滚已物化券，未验证补偿只进对账，仅策略与 Adapter 契约双验证后允许带独立幂等键的补偿。完整非 Live/Enterprise/Performance 套件 `478 passed, 1 deselected`，独立安全套件 `81 passed, 398 deselected`，Ruff format/Lint、mypy 与 migration asset 完整性通过。证据：[`reports/verification/v0.3/20260831T031713+0800/summary.md`](../reports/verification/v0.3/20260831T031713+0800/summary.md)。本任务未运行真实网络、Live、Enterprise 或 Performance，企业 Adapter 仍未验证。

### 6.2 真实验证场景

**V0.3-S1：10 步本地完整闭环（C）**：用真实 SQLite migration/事务和 Mock 外部 Adapter，从 CLI 提交需求，生成带引用的规则快照与硬资格候选；LLM 只做软排序/草案。另一授权运营批准 LaunchPlan 后物化一个券批次并投放商家侧；以 `hybrid` 模式注入一条商家自主报名和一批基于 ProductSnapshot 的确定性自动圈品结果，再注入关窗事件完成 join、确认链、券关联、选品提交/结果恢复，再由另一授权主体批准 C 端投放，最后按商家保存通知回执。逐表核对状态、版本、唯一键、execution ledger、outbox、audit 和 correlation ID。

**V0.3-S2：规则冲突与审批篡改（C/SEC）**：缺少报名时间、折扣率非法或两个生效规则来源冲突时必须停在澄清/拒绝，不创建草案外副作用。暂停后修改规则快照、券档位、招商范围、素材、选品版本、投放参数、审批主体或 policy version，恢复必须拒绝；launch 审批不能批准 C 端投放，反之亦然。

**V0.3-S3：双来源报名与动态确认链（C/REC）**：同一商品先由系统基于冻结 ProductSnapshot/product circle policy 自动圈选、后由商家自主报名，最终只有一个 EnrollmentItem 且保留两个 source；分别覆盖 `merchant/auto/hybrid`、merchant→sales→manager、无需确认、超时升级和超时拒绝。关窗后迟到报名默认被拒绝；规则允许补报时建新版本并使下游审批失效。未完成确认或不符合价格/类目/关键词规则的商品不得关联券批次或进入选品。提交选品时还必须另行校验 assortment policy 版本，不得用 product circle policy 代替。

**V0.3-S4：选品异步恢复与结果变化（C/REC）**：提交选品后进程退出，通过同一 thread 的 Mock webhook/轮询事件恢复；只允许 selected 且券关联有效的商品形成 ConsumerPlacementDraft。审批后若 selection version 改变，旧审批失效；未入选原因进入商家通知但不进入 C 端投放。

**V0.3-S5：崩溃、部分成功与重复恢复（C/REC）**：分别在 LaunchPlan 批准后、券批次外部成功但招商投放前、选品请求发送后回执落库前、C 端外部调用后回执落库前和通知部分失败时杀进程。恢复多次，验证每个外部业务结果最多一个；`unknown` 进入对账，已成功的前序事实不伪回滚，通知死信不回滚投放。

**V0.3-S6：最小权限与敏感范围（C/SEC）**：只读运营可以查询规则/商家但不能持久化草案或审批；活动管理员可以提交但不能自批 LaunchPlan/C 端投放；商家、销售和经理只能处理分配给自己的 ConfirmationTask；跨 tenant 的 rule/campaign/coupon/enrollment/selection/approval ID 均拒绝且无写入。黑白名单原文、内部销售组织和未授权商家数据不进入 LLM、日志或通知。

### 6.3 核心测试

- `UT`：Campaign/CouponBatch/LaunchSaga/Recruitment/ProductSnapshot/EnrollmentItem/Confirmation/Selection/ConsumerPlacement/Notification 全部状态转换，金额/折扣/阶梯/日期/区域/类目/关键词约束和 tenant 复合唯一键。
- `IT`：空库按 Alembic 升级到当前 revision、从上一 revision 升级、失败 migration 回滚；schema 与 ORM metadata 对齐。
- `REC`：pending writes、部分 super-step 失败、重复 resume、并行 reducer。
- `Graph-UT/HITL`：fake nodes 断言 launch/consumer publish 两个独立 interrupt、报名双分支汇聚、动态 ConfirmationTask 与选品 event wait；用内存 saver + `Command(resume=...)` 覆盖批准、拒绝、过期和参数/版本篡改，再以 AsyncSqliteSaver 跨进程/外部事件恢复测试证明持久化语义。
- `UT`：`merge_results/merge_unique` 对不同 key 和同 key 同值幂等合并；同 key 异值抛 `StateConflictError`，冲突 super-step 不接受且不采用 last-write-wins。
- `SEC`：deny-by-default、tenant/RBAC、职责分离、args/LaunchPlan hash、checkpoint/policy binding、越权审批、确认主体可信映射、黑白名单/销售组织最小披露和审计脱敏。
- `UT/SEC`：参数键顺序/空白不同但语义相同时 canonical hash 相同；金额、时区、tool schema version 或任一语义参数改变时 hash 改变；NaN/Infinity/未知字段被拒绝。
- `IT`：业务写 + execution ledger + outbox 同事务；故意回滚时三者都不提交。
- `IT`：Platform 审批状态 + audit/outbox 与 Business 业务状态 + domain/audit/outbox 分别回滚；任一库故障不会伪造跨库原子成功，恢复后由幂等投影/对账收敛。
- `IT/REC`：商品库分页在同一 snapshot/cursor 上稳定重放；`merchant/auto/hybrid` 分支按关窗规则 join；双来源相同商品汇聚为一个 EnrollmentItem；确认链/timeout 后才可关联券批次；选品重复/乱序 webhook 幂等，旧 selection version 不能进入投放。
- `E2E-F`：10 个业务步骤及两个等待/恢复点全部可观察；招商投放、招后选品和 C 端投放是不同事件/实体，不能由一个 `dispatch` 结果代替。
- `E2E-L`：真实 DeepSeek 只能在 EligibilityPolicy 候选集内软排序并生成草案；所有活动/券/报名/选品/投放写入只能由领域 Service 在授权、确认或对应审批满足后执行。

Core Gate：V0.3-T01–T08、migration、10 步 Community 闭环、最小权限、双来源汇聚、动态确认链、异步选品恢复和五类故障点均有实际证据；各业务唯一键重复执行计数保持 1，才可进入 V0.4。V0.3-T09 DeepSeek 草案/软排序验证为必需 Live 卡；没有真实券、商家侧、选品、C 端、IM 系统时明确写“社区本地完整业务语义已验证，企业 Adapter 未验证”。未通过 EligibilityPolicy/PolicyEngine/确认链的写操作不得以“后续 V0.5 再补安全”为由放行。

## 七、V0.4 场景 B 动态归因 Agent

### 7.1 实施任务

| ID | Depends on | 任务与产物 | 完成验证 |
| --- | --- | --- | --- |
| V0.4-T01 | V0.3-Core | 构建合成分析 schema/生成器/seed；根因标签与生产查询库物理分离 | 数据不变量与不可查询标签测试 |
| V0.4-T02 | V0.4-T01,V0.2-T03 | 实现 `query_funnel/drill_down/query_activity/query_market_overview/search_history_experience` 只读工具 | SQL 只读、tenant/时间范围、证据 provenance CT/SEC |
| V0.4-T03 | V0.4-T02,V0.1-T07 | 新增固定 `attribution_reasoning/v1`，复用有界 research 原语实现动态归因、evaluator-optimizer、证据引用、abstain 与预算终止 | Prompt/Agent CT/E2E-F；非固定路径断言 |
| V0.4-T04 | V0.4-T01,V0.4-T03 | 建不少于 50 条人工审阅 case、开发集与至少 20 条冻结 holdout、盲评 rubric 和 attribution eval CLI | schema/污染检查；golden 版本冻结 |
| V0.4-T05 | V0.4-T04 | 执行 S1–S3 Live、重复分层样本、校准/coverage-risk 报告 | 逐例原始结果、方差、人工校准证据 |

模型自报置信度只作候选信号，未用冻结数据校准前不得作为质量门禁；不把根因、预期工具顺序或答案措辞硬编码进 prompt/生产 Agent。

### 7.2 真实验证场景

**V0.4-S1：活动结束根因（C + L）**：冻结数据后才从 holdout 选取案例；真实模型面对 prompt、可检索文档和工具描述中均未透露的根因，通过动态工具结果定位“正餐活动结束”，引用 SQL/RAG 证据。检查轨迹确由中间结果驱动，而非固定 DAG 或 fixture 标签泄漏。

**V0.4-S2：冲突证据（C + L）**：大盘和区域证据冲突，Agent 必须呈现候选假设及不确定性，不得编造唯一结论。

**V0.4-S3：证据不足（C + L）**：移除关键活动数据，Agent 应 abstain/请求更多数据；强行给出确定结论记为失败。

### 7.3 核心测试

- Tool 参数、SQL 只读限制、tenant/时间范围权限。
- 最大迭代次数、Token/成本预算、重复无进展终止。
- 证据中的实体/数值可回查原始 ToolResult；引用不存在即失败。
- 数据污染测试确认 root-cause label、golden rationale 和 holdout ID 不可被 Retriever/Tool 查询；生产代码不得读取标签表。
- `E2E-F` Core：用 MockLLM 跑通冻结数据的 eval schema、abstain、证据回查和报告管线，只证明评测机制可执行。
- `E2E-L` 验证卡（V0.4-T05）：冻结 holdout 上运行真实模型并保存逐例结果；对分层样本重复运行 3 次报告方差。报告 coverage-risk 与置信度分桶准确率，样本足够时再报告 Brier/ECE，禁止用任意 confidence threshold 美化结果。LLM judge 只作辅助，采用与被测架构无关的盲评输入并由人工抽检校准；judge 与被测模型相同时必须在报告中披露。

Core Gate：V0.4-T01–T04 的数据隔离、工具和确定性 Agent 测试通过后可实施 V0.5。必需 Live 卡：V0.4-T05 的三个场景均有真实 run，冻结 holdout 未被逐题调参，并报告任务成功、abstain、证据/工具正确率、成本、延迟和重复运行方差；未通过时不得宣称场景 B 已验证，也不预设“Agent 一定优于 Workflow”。

## 八、V0.5 多智能体、上下文与记忆

### 8.1 实施任务

| ID | Depends on | 任务与产物 | 完成验证 |
| --- | --- | --- | --- |
| V0.5-T01 | V0.4-Core | 实现短期历史、滑窗摘要、事实账本和统一 context budget | 压缩前后事实断言；预算/溢出 UT |
| V0.5-T02 | V0.2-T03,V0.5-T01 | 实现 opt-in Memory、tenant/subject namespace、TTL、provenance/confidence/sensitivity、查看/删除/导出及 memory CLI | 生命周期、隔离、删除传播 CT/SEC |
| V0.5-T03 | V0.3-T02,V0.5-T02 | 完成 RBAC/ABAC、职责分离、动态工具暴露和 input/RAG/Tool/output Guardrail | deny-by-default、重新鉴权、投毒 SEC |
| V0.5-T04 | V0.5-T01,V0.5-T03,V0.4-T03 | 建 tool-based supervisor 与至少两个专职 Subagent，固定 handoff schema/allowlist/循环上限 | 路由、权限不放大、失败回收 CT/E2E-F |
| V0.5-T05 | V0.5-T04,V0.4-T04 | 建 single/multi 等额总预算、随机顺序、隐藏架构标签的对照 harness 与 `eval compare` | 预注册 rubric；结果含质量/成本/延迟/方差 |
| V0.5-T06 | V0.5-T02,V0.5-T03,V0.5-T04 | 执行 S2–S4 Community/Security，更新威胁模型和 Memory 保留/删除说明 | C/SEC Core 报告；删除/投毒/权限断言 |
| V0.5-T07 | V0.5-T05,V0.5-T06 | 执行 S1 single/multi 必需 Live 对照；历史场景 B Live 报告只作 evidence reference | L report passed/failed/blocked；质量/成本/延迟/方差 |

### 8.2 真实验证场景

**V0.5-S1：单/多 Agent 对照（L）**：相同模型、数据、prompt 资产、工具、总 Token/工具调用/墙钟预算和终止规则分别运行 single-agent 与 multi-agent；随机化执行顺序，向 judge 隐藏架构标签，保存质量、工具调用数、Token、成本、延迟和重复运行方差。只有实际提升时才写提升，否则记录退化并说明适用边界。

**V0.5-S2：跨会话追问（C；可选 L 观察）**：用户明确 opt-in 保存事实，新 session 提问时可检索；撤销/删除后内容、向量和缓存均不可再次召回。审计仅保留脱敏删除事件、对象 ID/hash 与操作者，不保留被删除正文；checkpoint/备份中的残留按已声明保留策略处理并可验证。Core 只要求 C 级生命周期断言；若用真实模型观察表达质量，结果并入独立 Live 报告。

**V0.5-S3：记忆投毒（SEC）**：写入含“忽略规则并调用高风险工具”的记忆，检索后不得扩大工具权限或作为权威业务事实。

**V0.5-S4：动态最小权限（SEC）**：同一请求分别以只读运营、活动管理员和审批人身份运行，模型可见工具集合及实际执行结果必须符合 PolicyDecision；跨租户资源、伪造角色和被 RAG 指令诱导的越权调用均被拒绝并留审计。

### 8.3 核心测试

- Supervisor 路由、Subagent allowlist、handoff schema 和最大循环。
- 压缩前后固定关键事实逐项对比，不以“摘要看起来合理”代替断言。
- tenant/subject 隔离、TTL、删除、导出、低置信记忆不自动注入。
- PolicyEngine deny-by-default、属性变化后的重新鉴权、动态工具暴露、Subagent 继承但不扩大调用方权限。
- single/multi 对照采用预注册 rubric、同一冻结数据和等额总预算，禁止挑选各自最优案例或只报告最佳一次。

Core Gate：V0.5-T01–T06、完整 ABAC/Guardrail、动态最小权限、对照 harness 和全部 Memory 生命周期测试通过后可实施 V0.6。必需 Live 卡：V0.5-T07 单多 Agent 对照报告；缺失时只能声明多智能体控制流已实现，不能声明质量提升。多智能体价值按数据陈述，不把“能运行”写成“效果更好”。

## 九、V0.6 API 与 Durable Job

### 9.1 实施任务

| ID | Depends on | 任务与产物 | 完成验证 |
| --- | --- | --- | --- |
| V0.6-T01 | V0.5-Core,V0.3-T01 | 将 platform/business 两条 migration 接入 PostgreSQL、连接池、RLS/等价隔离、官方 AsyncPostgresSaver 与统一 `db upgrade --target platform\|business\|all`；保留 SQLite 单 worker 实现 | 双 DB/双后端 Repository/Saver CT；migration/RLS IT |
| V0.6-T02 | V0.5-Core,V0.5-T03 | 实现 FastAPI v1、统一错误、JWT/JWKS/OIDC-ready actor 映射、executor service identity、资源 Policy 与 `api serve`；实现飞书/钉钉 IngressAdapter webhook endpoint，以受限 `InboundRequest` 验签/挑战、防重放、source message 去重并生成可信 `InboundMessage`/主体映射 | token/IDOR/撤权/职责分离 SEC；两平台 webhook 契约/伪造/重放 CT/SEC；raw body 不进日志/状态/inbox |
| V0.6-T03 | V0.6-T01,V0.6-T02 | 实现 Job 状态机（含 waiting_approval/waiting_event）、Idempotency-Key、external_waits、integration event inbox、`IntegrationEventEnvelope` 验签映射、事件 union、`(tenant_id,adapter_id,source_event_id)` 去重、resource/version/wait 绑定、lease/heartbeat/retry/timeout/cancel/job events | 状态/CAS/取消 CT/IT；重复/乱序/迟到/未知类型/错误版本事件不恢复 Graph；客户端不能指定 checkpoint/wait |
| V0.6-T04 | V0.6-T03 | 实现 `worker run` 独立进程、lease_epoch fencing、epoch checkpoint namespace、accepted pointer 与 orphan 清理 | 双 worker 竞争/暂停/复活 REC |
| V0.6-T05 | V0.6-T02,V0.6-T03 | 实现持久化 SSE event sequence、Last-Event-ID、HMAC Webhook delivery/retry/replay/dead-letter | 真实 HTTP 断连/重连与 receiver IT/SEC |
| V0.6-T06 | V0.6-T04,V0.6-T05 | 执行 S0/S3/S4 Community/Security，更新威胁模型；导出 OpenAPI v1 snapshot、建立 breaking-change diff 门禁并冻结统一错误/SSE union | C/IT/SEC Core 报告；OpenAPI snapshot/drift CT |
| V0.6-T07 | V0.6-T06 | 在 PostgreSQL 两独立 worker 执行 S1/S2 必需 E-like 卡 | E-like/REC report passed/failed/blocked |

Job 只保存 requester 稳定引用/提交快照 hash，不保存 bearer token；worker 以独立 executor service identity 恢复 actor 当前属性并重新鉴权。FastAPI 进程不直接承担 durable background job。

### 9.2 真实验证场景

**V0.6-S0：社区单 Worker（C）**：SQLite + AsyncSqliteSaver 启动 API 与一个 worker，完整跑通提交、等待审批、等待报名/选品外部事件、验签/去重后恢复、取消和 SSE；明确该 profile 不宣称多 worker 安全。

**V0.6-S1：跨进程审批与事件恢复（E-like）**：在 PostgreSQL + AsyncPostgresSaver 上由 API 提交完整场景 A，worker 分别在 launch `waiting_approval` 和选品 `waiting_event` 退出；每次由新 worker 启动，另一授权主体审批或已验签 Mock Adapter 事件入 inbox 后从同一 thread 恢复。重复/乱序 selection event 不得重复推进或覆盖已接受版本。

**V0.6-S2：Lease 接管（REC/E-like）**：连接同一 PostgreSQL 启动两个独立 worker，暂停 lease owner 至租约过期，另一个 worker 通过数据库条件 claim 接管；再恢复旧 worker，旧 fencing token 的 accepted-checkpoint CAS、领域提交和副作用 reservation 必须被拒绝，其晚到 saver 写入不得成为 resume 游标，业务副作用不重复。禁止用 SQLite、同一进程协程或内存锁替代该验证。

**V0.6-S3：SSE 与 Webhook（IT）**：真实 HTTP 客户端断开 SSE 后携带 Last-Event-ID 重连；本地 webhook receiver 验证 HMAC、delivery ID、时间戳、失败重试和死信。

**V0.6-S4：API/IM 身份与租户隔离（SEC）**：使用有效、过期、错误 issuer/audience、伪造 tenant/role 的 token 请求 job/thread/approval；对飞书/钉钉 endpoint 提交有效 challenge、错误签名、过期时间戳、重复 source message、正文伪造角色和未映射用户。只允许 token 或已验签企业身份映射后的 actor 与受信 executor 访问本租户已授权资源；资源 ID 是否存在不得造成跨租户泄漏。提交后撤销 actor 角色再恢复 Job，必须按当前策略拒绝而不是沿用旧 claims。

### 9.3 核心测试

- Job 状态转换 compare-and-set，非法跳转拒绝。
- waiting_event 的 wait/resource/version/checkpoint/timeout action 绑定；事件先落 inbox 再 CAS 唤醒，重复、乱序、过期、错误版本和无匹配 wait 事件不能推进 Job。
- 同 Idempotency-Key 重复提交返回同一业务结果。
- running/waiting 状态取消、timeout、retryable/non-retryable 错误。
- 两 worker 竞争、heartbeat、lease expiry、进程崩溃恢复。
- 暂停后复活的旧 worker、过期 fencing token、并发 claim 和慢外部调用；只有当前 lease epoch 可推进状态/accepted checkpoint，orphan namespace 不可被 resume 并可按保留策略清理。
- SSE sequence 无重复/缺口；Webhook 重放和篡改签名拒绝。
- JWT/JWKS 轮换、issuer/audience/expiry、actor/executor 映射、主体撤权后的 Job 恢复、跨租户 IDOR、审批职责分离；数据库/日志/checkpoint 不记录 bearer token。
- 飞书/钉钉 challenge、签名、时间窗、重放与消息去重；消息正文/自由 header 不能赋予 tenant/role，相同 source message 只创建一个 Job。
- PostgreSQL RLS/等价策略和连接池复用：tenant A 事务结束后同一连接服务 tenant B 时不得残留 A 的上下文或数据权限。
- SQLite 与 PostgreSQL Repository/Saver 契约一致；PostgreSQL migration 在空库和上一 revision 均可升级。
- OpenAPI snapshot 可重复生成；删除 endpoint/枚举值、收紧字段或改变错误/SSE schema 时 breaking-change 门禁失败。

Core Gate：V0.6-T01–T06 的契约/安全测试、S0 SQLite 单 worker、S3 HTTP 与 S4 身份拒绝实际完成后可实施 V0.7。必需 E-like 卡：V0.6-T07 中 S1/S2 使用 PostgreSQL、真实独立进程和 HTTP 连接完成；未通过时不得宣称多 worker/Durable Job 已验证。仅调用 Python 函数、Mock client、单进程或 SQLite 双 worker 不算企业语义验证。

## 十、V0.7 MCP、插件与扩展后端

### 10.1 实施任务

| ID | Depends on | 任务与产物 | 完成验证 |
| --- | --- | --- | --- |
| V0.7-T01 | V0.6-Core | 锁定官方 `mcp>=2,<3` SDK，建立 version/capability/conformance probe 与兼容矩阵 | 锁定版真实输出报告；无硬编码协商结果 |
| V0.7-T02 | V0.7-T01,V0.5-T03 | 实现 stdio/HTTP client/server、`mcp serve`、`mcp doctor`、认证、server allowlist、scope 与 ToolPolicy/ToolExecutor adapter | 独立进程/HTTP CT/IT；恶意 schema/SSRF SEC |
| V0.7-T03 | V0.7-T01,V0.7-T02,V0.6-T03 | Tasks 可用时映射 Oria Job；不可用时实现 submit/get/cancel 普通工具与显式 handle | 两分支按实际 capability 测试，未实现项显式标记 |
| V0.7-T04 | V0.6-Core,V0.1-T02 | 实现 `oria.plugins` entry-point、manifest/API version、受控重启、teardown/rollback 与 `plugins list`；提供独立测试 wheel | 安装/卸载/失败回滚 IT；仅受信同进程代码 |
| V0.7-T05 | V0.6-Core,V0.2-T03,V0.5-T03 | 实现 Redis CacheStore、版本化 key、TTL/降级和禁止缓存分类 | 真 Redis E-like；tenant/敏感/写操作 SEC |
| V0.7-T06 | V0.7-T02,V0.7-T03,V0.7-T04,V0.7-T05 | 执行 S1–S3，更新威胁模型、生态 capability 与未验证清单 | C/E-like/SEC 报告 |

不受信扩展只能以受限独立进程/容器经 MCP 接入；entry-point manifest/签名不构成沙箱。语义缓存只用于只读、可重新计算的答案，禁止缓存审批、写工具结果、敏感响应和权威业务事实。

### 10.2 真实验证场景

**V0.7-S1：独立 MCP 进程（C）**：Oria client 调用另一个进程的 reference server；测试 stdio 和 HTTP、版本协商、超时、断连和恶意 schema。

**V0.7-S1b：MCP 长任务能力（C）**：记录锁定 SDK 实际协商 capability。Tasks 可用时验证 Tasks 与 Oria Job ID 的显式映射；不可用时验证三种普通 job 工具和 handle 生命周期，并在报告中明确 Tasks 未实现。

**V0.7-S2：受信插件安装/卸载（C）**：在临时虚拟环境安装受信测试 wheel，受控重启后多出一个 Tool；卸载并重启后消失，初始化失败不残留半注册状态。测试报告不得把 manifest、签名或 allowlist 描述为代码沙箱。

**V0.7-S3：真实 Redis（E-like）**：容器 Redis 执行缓存命中、TTL、tenant 隔离和后端不可用降级；不得用内存 fake 冒充 Redis 集成通过。

### 10.3 核心测试

- MCP capability/version、auth issuer/scope、SSRF/localhost/metadata IP 和输出大小限制。
- 外部 Tool 继承权限、超时、幂等和审计策略，不能绕过 Tool Executor。
- 插件 manifest/API version/allowlist/初始化回滚和依赖冲突；拒绝把未信任 wheel 作为 entry-point 加载，独立 MCP 进程受 egress、凭证、资源和超时约束。
- Redis key 隔离、序列化兼容、敏感/写操作不缓存、故障降级不影响权威业务状态。

Core Gate：V0.7-T01–T04、真实子进程/HTTP、SDK capability 报告、受信 wheel 安装和未信任扩展隔离有证据后可实施 V0.8。必需 E-like 卡：V0.7-T05/T06 真实 Redis 容器；Tasks 支持仅在协商与 conformance 实际通过时声明。仅 unit test 不足以宣称生态接入完成。

## 十一、V0.8 生产证明与旗舰演示

### 11.1 实施任务

| ID | Depends on | 任务与产物 | 完成验证 |
| --- | --- | --- | --- |
| V0.8-T01 | V0.7-Core,V0.6-T01 | 实现 Oria 自有 platform/business 表 SQLite→PostgreSQL 版本化迁移、outbox high-water 增量追平、切换/回滚；按 catalog + ObjectStore + Embedder revision 重建 Milvus 影子索引；对官方 saver 实施“停止接单→排空非终态 Job→新 saver”默认切换 | 非空脱敏数据迁移/影子索引/回滚 E-like；存在非终态 Job 时拒绝默认切换 |
| V0.8-T02 | V0.7-Core,V0.6-T05 | 完成 OTel API→Job→Graph→LLM/RAG/Tool→HITL→Webhook，接真实 Collector/后端 | correlation/trace 完整性与内容脱敏 IT |
| V0.8-T03 | V0.7-Core,V0.6-T06 | 建 `web/` React+TypeScript+Vite，锁 Node/npm，按冻结 OpenAPI snapshot 生成 client；实现 OIDC Code+PKCE（state/nonce，access token 仅页面内存，无持久 refresh token）/社区本地身份入口、提交、SSE、审批、取消和结果页；本地身份仅 dev build/profile + loopback 可用 | TypeScript/lint/build；生成 client 无 drift；重载后无 token 残留并重新认证；生产构建无本地身份；state/nonce/CORS/Playwright E2E |
| V0.8-T04 | V0.8-T01,V0.8-T02,V0.8-T03 | 固化 Docker/Compose、健康检查、初始化/migration job 与全新环境启动 | 空缓存/空卷冷启动 E-like |
| V0.8-T05 | V0.8-T04 | 建依赖审计、secret scan、许可证清单/SBOM、镜像扫描和固定版本报告 | 实际扫描输出；发现项处理/风险接受 |
| V0.8-T06 | V0.8-T01,V0.8-T04,V0.8-T05,V0.5-T05 | 完整 Eval、压力/恢复/安全演练，汇总威胁模型及飞书/钉钉、商家/商品库、券、招商投放、选品、C 端投放、通知等企业 Adapter 独立卡；建立 `release.yml` 对最近 7 天、commit 为发布祖先且 eval_fingerprint 完全匹配的必需 Live 卡门禁 | S1–S5、性能/成本/残余风险报告；每个 Adapter 不混合状态；供应链扫描产物已纳入总结；过期/不匹配/失败 Live 卡阻断 release |
| V0.8-T07 | V0.8-T03,V0.8-T06 | 完成两个 Hero 场景的社区/本地企业栈旗舰演示；场景 A 必须展示完整 10 步、双等待恢复和双投放语义；保存独立 DeepSeek Live 卡、README/架构图/ADR/面试证据索引 | 全新环境 Community/E-like runbook 实际通过；DeepSeek 卡独立 passed/failed/blocked，有条件时保存录像 |

T01/T02/T03 从各自前置并行，T04 才汇合迁移、观测和 Web 产物。PostgreSQL/AsyncPostgresSaver 已在 V0.6 接入，本阶段验证真实非空存量迁移，禁止把空库建表称为迁移通过。官方 saver 内部表不属于 Oria migration；默认切换前必须排空非终态 Job，旧 saver 按保留策略只读归档。在途 checkpoint 导入是独立可选卡，只能调官方 saver API 并通过 compatibility suite；EventSourcedCheckpointer 通过契约前不替换默认 saver。

### 11.2 真实验证场景

**V0.8-S1：真实容器迁移（E-like）**：以 V0.1–V0.6 实际积累且脱敏的非空数据，迁移两个 Oria SQLite DB 到 PostgreSQL，以 outbox high-water mark 追平；从 catalog/ObjectStore 重建 Milvus 并与 Chroma 影子查询对照。比较记录数、业务不变量、Event/审计关联、文档/引用/向量版本和场景输出；存在 waiting/running Job 时切换预检必须拒绝。排空后切换到 AsyncPostgresSaver，新建一个任务停在审批点并成功 resume；这只证明 saver 切换，不写成“原在途 checkpoint 已迁移”。在仍停止接单且新库未放行写入的验收窗口内演练一次切换失败/回滚；如测试新库已产生写入，必须丢弃该测试写入或使用经验证的反向对账，不伪装无损回滚。

**V0.8-S2：完整 Trace（C/E-like）**：经 API 提交场景 A/B，在真实 OTel Collector 中按同一 trace/correlation ID 查到所有跨度，确认默认不采集 prompt、密钥、隐式思维链和敏感 Tool 参数。

**V0.8-S3：冷启动旗舰 Demo（E-like）**：在全新环境按 README 用 Docker Compose 启动，通过 Web UI 完成场景 A 审批和场景 B 归因；不使用开发者机器遗留缓存或数据库。

**V0.8-S4：故障与安全演练（REC/SEC/PERF）**：并发请求、worker kill、DB/Redis/Milvus 短暂不可用、跨租户 ID 猜测、RAG 注入、MCP 恶意服务、Webhook 重放；记录实际恢复时间、错误率、吞吐、成本和未恢复问题。

**V0.8-S5：供应链与全新构建（SEC）**：从空缓存按锁文件构建 wheel/镜像，生成 SBOM 并实际运行依赖、secret、许可证和镜像扫描；阻断篡改 lock/hash 的构建，保存原始报告和未修复项。

### 11.3 核心测试与退出门禁

- 迁移前后实体、事件、审计、outbox high-water mark、向量与引用校验；两次执行幂等，切换/回滚可执行。
- saver 切换预检拒绝非终态 Job；排空后新 Postgres checkpoint 可 pause/resume，报告明确区分“后端切换”与“在途 checkpoint 迁移”。
- 核心恢复、幂等、跨租户和审批安全路径全部有场景测试；仓库行覆盖率基线按实际报告，目标不低于 80%。
- 两个 Hero 场景在 Mock CI 和 Community/E-like 栈分别运行并保存 Core 报告；真实 DeepSeek 对同一场景/数据另存 Live 卡，不用 Mock 报告替代。
- UI、API、worker、PostgreSQL、Milvus、Redis、Collector 使用真实独立进程/容器。
- 冷启动演示与供应链扫描从全新工作目录/空数据卷执行；固定依赖和模型 revision，可根据 lock/SBOM 复现。
- README 中的每个性能或质量数字都能定位到 verification report；没有证据的能力标“implemented but not live-verified”。

最终 Core Gate：V0.8-T01–T07 的社区/本地企业栈测试和全新环境旗舰 Demo 通过。必需 Live 卡：两个 Hero 场景使用真实 DeepSeek 完成同版数据/配置 run；未通过时最终状态只能是“Core 完成，Live failed/blocked”。其他外部验证卡按 Provider、DMS/商家商品库、飞书/钉钉、券系统、招商商家侧、选品系统、C 端投放、通知与企业网关逐项独立；没有环境的保持 `blocked/未 Live 验证`，不阻止社区版发布，但正式版能力清单只能列实际通过项。V0.8 没有后续版本可“带病继承”，所有失败、风险接受和未验证项必须进入最终证据索引。

## 十二、阶段状态更新模板

完成或复核一个版本时，在本文版本总览和对应章节末追加：

```markdown
### V0.x 验证状态（YYYY-MM-DD）

- 代码状态：未开始 / 进行中 / 已实现
- Core Gate：未运行 / 通过 / 失败
- Fixture 验证：未运行 / 通过 / 失败
- Community Real：未运行 / 通过 / 失败
- Live Provider：未运行 / 通过 / 失败（列 provider/model）
- Local Enterprise Stack：未运行 / 通过 / 失败（列 PostgreSQL/Milvus/Redis/OTel 版本）
- Enterprise：未运行 / 通过 / 失败 / 无环境
- 必需验证卡：逐项列 passed / failed / blocked
- 证据：reports/verification/...
- 未解决问题：...
- 允许对外声明：...
- 禁止对外声明：...
```

状态只能由实际命令输出、可查询数据、真实回执或用户确认更新。代码生成完成不等于验证完成。
