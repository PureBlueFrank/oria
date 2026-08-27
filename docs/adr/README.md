# Oria 架构决策记录

本目录是 ADR 状态的权威索引。架构主文档中的 ADR 编号只表示决策主题；只有在本索引中标为“已接受”且有实体文件的 ADR，才是已冻结决策。

## 状态定义

- **计划中**：已识别决策主题，但尚未评审或实体化；必须在对应实施任务开始前补齐。
- **提议中**：已有文档和候选方案，正在评审，不可作为已冻结实现依据。
- **已接受**：边界、后果和验证要求已冻结，实现必须遵循。
- **已替代**：历史决策，仅供追溯；索引必须指向替代它的 ADR。
- **已废弃**：不再采用且无直接替代方案。

## 索引

| ADR | 主题 | 状态 | 文件 / 实体化门禁 |
| --- | --- | --- | --- |
| ADR-001 | Provider 归一化 | 已接受 | [ADR-001](ADR-001-provider-normalization.md) |
| ADR-002 | Chroma 到 Milvus | 计划中 | V0.8-T01 前实体化 |
| ADR-003 | SQLite 到 PostgreSQL | 计划中 | V0.6-T01 前实体化 |
| ADR-004 | Checkpoint 幂等与恢复 | 计划中 | V0.1-T03 前实体化 |
| ADR-005 | NodeResult 与 Job Schedule 分离 | 计划中 | V0.1-T02 前实体化 |
| ADR-006 | Workflow 与 Agent 选择 | 计划中 | V0.1-T06 前实体化 |
| ADR-007 | RAG 与 fine-tune | 计划中 | V0.2-T02 前实体化 |
| ADR-008 | Reranker 效果验证 | 计划中 | V0.2-T04 前实体化 |
| ADR-009 | 多智能体边界 | 计划中 | V0.5-T04 前实体化 |
| ADR-010 | Guardrails 与 HITL | 计划中 | V0.3-T02 前实体化 |
| ADR-011 | RAG 权限 pre-filter | 计划中 | V0.2-T03 前实体化 |
| ADR-012 | Memory 生命周期 | 计划中 | V0.5-T02 前实体化 |
| ADR-013 | 端到端流式事件 | 计划中 | V0.6-T05 前实体化 |
| ADR-014 | Durable Job | 计划中 | V0.6-T03 前实体化 |
| ADR-015 | Eval 子系统 | 计划中 | V0.1-T09 前实体化 |
| ADR-016 | 薄核心与插件 seam | 计划中 | V0.7-T04 前实体化 |
| ADR-017 | Context + Protocol | 计划中 | V0.1-T02 前实体化 |
| ADR-018 | Checkpoint 与 Domain/Audit Event 分离 | 计划中 | V0.1-T03 前实体化 |
| ADR-019 | 插件与 MCP 分层 | 计划中 | V0.7-T01 前实体化 |
| ADR-020 | 多租户隔离 | 计划中 | V0.2-T03 前实体化 |
| ADR-021 | 成本预算门禁 | 计划中 | V0.1-T06 前实体化 |
| ADR-022 | Prompt 版本管理 | 计划中 | V0.1-T06 前实体化 |
| ADR-023 | Supabase 到 DMS | 计划中 | 企业商家/商品 Adapter 开发前实体化 |
| ADR-024 | HITL 后副作用幂等 | 计划中 | V0.3-T03 前实体化 |
| ADR-025 | Job lease/fencing 恢复 | 计划中 | V0.6-T03 前实体化 |
| ADR-026 | 招商领域模型 | 计划中 | V0.3-T01 前实体化 |
| ADR-027 | RAG/Memory 内容安全 | 计划中 | V0.2-T03 前实体化 |
| ADR-028 | 确定性资格与 LLM 软排序 | 已接受 | [ADR-028](ADR-028-deterministic-eligibility-and-llm-ranking.md) |
| ADR-029 | 外部事件等待与恢复 | 已接受 | [ADR-029](ADR-029-external-event-wait-and-resume.md) |
| ADR-030 | 跨 seam 值类型深度不可变 | 已接受 | [ADR-030](ADR-030-deep-immutable-seam-values.md) |

新建 ADR 使用 [ADR 模板](000-template.md)。已接受 ADR 如需改变关键边界，应新建 ADR 并把原记录标为“已替代”，不直接覆盖历史理由。
