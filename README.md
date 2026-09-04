# Oria

Oria 是面向招商活动编排的 AI Agent 平台：用 LLM 处理需求理解、草案和候选集内软排序，用确定性规则、权限、审批、幂等和审计守住业务边界。

当前可用的主线是可中断、可恢复的招商 Workflow；零配置 Demo 使用 Mock LLM 和合成数据，真实 LLM 仅有 DeepSeek 已完成 Live 验证。券、招商、商品库、选品、C 端投放和 IM 仍是 Mock Adapter；动态归因已完成合成数据基础和五个只读分析 Tool，Agent 尚未实现。

## 60 秒体验

需要 Python 3.11 和 uv 0.12.6。以锁文件同步依赖，再运行默认 human 输出的只读提案：

```bash
uv sync --locked --group dev
uv run oria demo
```

一次典型终端输出如下（ID 和路径每次不同）：

```text
$ uv sync --locked --group dev
Resolved 156 packages in <time>
Checked 121 packages in <time>
$ uv run oria demo
Oria offline demo completed
Correlation: corr_<generated>
Eligible merchants: 10
Proposal report: <data-dir>/reports-tmp/run_<generated>.json
```

`demo` 会自动迁移本地 SQLite、播种 12 家合成商家、建立 Chroma 投影，再运行带逐字段引用的招商提案。默认不需要账号、Key、网络或企业服务，也不会创建 Campaign/CouponBatch 或执行任何业务投放。

## 选择你的路径

| 路径 | 适合谁 | 依赖 | 入口 | 能证明什么 |
| --- | --- | --- | --- | --- |
| 零配置 Demo | 首次了解 Oria | 核心依赖，无 Key | `uv run oria demo` | Mock/Fixture 下的只读提案、引用和硬资格边界 |
| 真实 DeepSeek | 体验真实模型草案/软排序 | `standard` extra、DeepSeek Key、首次 BGE 下载 | [真实 LLM 快速开始](docs/guides/real-llm.md) | 指定 DeepSeek 模型与本地 BGE 的调用；不证明企业 Adapter |
| 完整本地 Workflow | 评估 10 步流程、HITL 和恢复 | 本地 SQLite、合成数据、Mock Adapter | [本地 Workflow 手册](docs/guides/local-workflow.md) | Community 业务语义、双审批/双等待与幂等对账 |
| 开发验证 | 贡献者和架构评审者 | 开发依赖 | `make lint && make test` | 无 Live/Enterprise/Performance 的本地回归与静态门禁 |

## Workflow 十步概览

| 步骤 | Oria 做什么 | 何时需要人 | 产物 |
| --- | --- | --- | --- |
| 1. 需求受理 | 规范化请求并校验本地可信主体 | 输入招商目标 | `CampaignIntent` |
| 2. 规则快照 | 检索六类规则并固化逐字段引用 | 规则缺失或冲突时澄清 | `CampaignRuleSnapshot` |
| 3. 商家预筛 | 用 `EligibilityPolicy` 执行确定性硬资格过滤 | 通常不需要 | 合格候选集与排除摘要 |
| 4. 软排序与草案 | LLM 仅在合格候选集内排序、解释并生成草案 | 未决项需补充 | 活动/券草案与商家排序 |
| 5. 招商投放 | 绑定 LaunchPlan，幂等物化券并投放商家侧 | 运营审批 `launch_approval` | 券批次回执与招商投放回执 |
| 6. 报名/圈品汇聚 | 合并商家报名与系统圈品，等待关窗 | 商家发起报名 | 去重的 `EnrollmentItem` |
| 7. 业务确认与券关联 | 按冻结规则运行动态确认链，再关联券批次 | 内置 Fixture 需商家→销售→经理三级确认 | `ConfirmationTask` 与券关联 |
| 8. 异步选品 | 提交选品并等待受信结果事件 | 外部系统返回结果 | `AssortmentSubmission` / `SelectionDecision` |
| 9. C 端投放 | 只纳入已入选且券关联有效的商品 | 审批 `consumer_publish_approval` | `ConsumerPlacement` 与回执 |
| 10. 通知闭环 | 按商家发送结果，失败进重试/死信 | 非标准或敏感通知时升级 | 通知回执、审计与对账证据 |

完整本地样例会先停在 Launch 审批，经报名关窗、3 轮业务确认和选品事件后，再停在 C 端投放审批；终态为 `status: completed` 且 `interrupts: []`。所有命令、ID 来源和拒绝分支见 [本地 Workflow 操作手册](docs/guides/local-workflow.md)。

## 架构与关键约束

```mermaid
flowchart TB
    U[用户请求] --> I[CLI / API / Ingress]
    I --> J[Durable Job / Retry / HITL / External Wait]
    J --> W[LangGraph Workflow / Agent Loop]
    W --> M[LLM Provider / Tool Router]
    M --> K[RAG / Knowledge]
    W --> D[招商领域服务]
    D --> A[企业系统 Adapter]
    W -. checkpoint / resume .-> C[(Checkpoint DB)]
    J -. approvals / audit .-> P[(Platform DB)]
    D -. domain events / outbox .-> B[(Business DB)]
    G[Policy / Guardrails / Secrets] -. 横切治理 .-> W
    O[Observability / Eval / Cost] -. 证据 .-> W
```

- Checkpoint 记录“执行到哪里”，Domain/Audit Event 记录“发生了什么”，二者不互相代替。
- 硬资格由确定性 Policy 判定；LLM 不能放宽规则、直接写库或选择审批人。
- 招商投放与 C 端投放是两个独立副作用和两道独立 HITL。
- 副作用用参数哈希、业务幂等键、execution ledger、outbox 和对账收敛。
- 读写都绑定 tenant、actor/executor 与 PolicyDecision，RAG 在召回前强制 ACL 过滤。
- 合成数据、Fixture、Community、Live 和 Enterprise 证据分层记录，Mock 结果不会冒充真实接入。

更完整的分层和数据边界见 [架构概览](ARCHITECTURE.md) 与 [Oria 架构设计](Oria架构设计.md)。

## 验证状态与限制

- V0.3 T01–T09 已完成；T09 DeepSeek Live 卡于 2026-09-03 通过，证据见 [V0.3-T09 验证报告](reports/verification/v0.3/20260903T004622+0800/summary.md)。
- 该 Live 卡只验证 `deepseek-v4-flash` 对本地合成规则/商家数据的草案和候选集内软排序；Kimi、智谱、OpenAI 和 Anthropic 仍只有 Fixture 契约。
- 完整场景 A 已通过本地 SQLite、AsyncSqliteSaver、合成数据和 Mock Adapter 验证；真实券、招商、商品库、选品、C 端投放和 IM 未验证。
- SQLite Community 结果不证明 PostgreSQL 多 worker、企业网络、SSO、网关或生产 SLA。V0.4 T01–T02 已完成，动态归因 Agent、冻结评测集和 Live 质量验证尚未实现。

## 开发与文档导航

```bash
make lint
make test
make build
make smoke
```

`make test` 不运行 Live、Enterprise 和 Performance 标记。这些验证必须显式提供运行开关、非空已知 target 与所需凭证/组件，不能把 skip、Mock 或 Fixture 记为通过。

- 上手：[真实 DeepSeek](docs/guides/real-llm.md) · [完整本地 Workflow](docs/guides/local-workflow.md)
- 参考：[数据模型与核心表](docs/reference/data-model.md) · [ADR 索引](docs/adr/README.md) · [威胁模型](docs/security/V0.3场景A威胁模型.md)
- 规划与证据：[详细执行路线](docs/Oria详细执行路线.md) · [执行计划](ROADMAP.md) · [验证证据模板](reports/verification/TEMPLATE.md)

依赖必须通过 `uv.lock` 同步。仓库不提交密钥、令牌、真实客户数据或 `.env` 文件。
