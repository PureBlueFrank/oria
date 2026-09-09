# ADR-012：长期记忆采用 opt-in memory-as-tool，与短期背景摘要分层

- 状态：已接受
- 日期：2026-09-10
- 决策者：FrankLee
- 关联任务：V0.5-T02（依赖 V0.2-T03、V0.5-T01）

## 背景

V0.5-T01 已交付会话级上下文治理：短期历史、滑窗摘要、事实账本与统一 context budget，其中 `compress` 用确定性规则对短期对话做背景摘要。T02 需要引入跨会话长期记忆。长期记忆的写入方式有两种根本不同的路径：自动背景保存，或显式工具调用。

## 候选方案

1. **自动背景保存**：系统在对话过程中自动把摘要/事实写入长期记忆，无需用户或模型显式动作。优点是无感；缺点是隐私边界模糊（用户无法确知什么被存了）、删除与审计压力大、投毒面更广，且与"权威业务事实从业务系统读取、模型记忆不得覆盖权威事实"的原则冲突。
2. **memory-as-tool（显式 opt-in）**：只有模型或用户通过 `save_memory` 工具（或 CLI）明确请求时才写入；检索通过 `search_memory` 显式进行。
3. **混合**：自动背景摘要 + 部分 opt-in 显式记忆，但两套写路径并存在治理和审计上最复杂。

## 决策

采用方案 2，并与 T01 的短期背景摘要形成分层：

- **会话内（自动、短期）**：T01 的 `InMemoryMemory` 短期历史 + 确定性压缩 + 事实账本，仅服务于当前会话，随 checkpoint 恢复，不进长期库。
- **跨会话（opt-in、长期）**：T02 的 `PersistentMemory`，只有 `save_memory`（或 `memory save` CLI）显式写入才持久化到 platform DB 的 `memory_items` / `memory_embeddings`；`save_fact` 强制 `opt_in=True`，非 opt-in 写入抛 `PermissionError`。

具体约束：

- **命名空间与隔离**：所有读写按 `(tenant_id, subject_id)` 隔离，查询强制带两者，deny-by-default；跨 namespace 写入被 `_authorize` 拒绝。
- **生命周期**：`expires_at` TTL 到期后 `search`/`view`/`export` 均不可见；`delete` 同步清空正文、删除向量投影、失效搜索缓存，审计 `audit_events` 仅记录脱敏删除事件（`object_hash`），不保留被删除正文。
- **provenance/confidence/sensitivity**：高敏感（非 `low/public/internal`）内容拒绝保存；`confidence < 0.7` 的记忆不自动注入检索结果（`view` 仍可见，便于治理）。
- **投毒边界**：`search_memory` 返回的每条记忆标记 `trust_level="untrusted_data"`、`authority="non_authoritative"`，不得据此扩大工具权限、绕过 system 指令或覆盖业务系统事实；工具描述明示该约束。
- **脱敏**：保存时对凭证（api_key/token/password/secret/bearer/authorization）、邮箱、电话做正则脱敏，正文存脱敏后的 `[REDACTED]`。
- **向量投影**：复用 `embedder` 生成向量，存 `memory_embeddings`（与正文同生命周期，删除同步）；检索用余弦相似度排序，社区版不依赖 Chroma。

## 后果

- 正向：隐私边界清晰（opt-in）、删除/审计可验证、投毒面受限、与"LLM 只叙述不判断、权威事实从业务系统读取"的确定性原则一致。
- 代价：长期记忆质量依赖模型/用户显式保存，不自动积累；启发式向量检索不等于语义理解；SQLite 不保留时区，`expires_at` 读回时统一按 UTC 恢复。
- 迁移/回滚：`platform_0008` 新增 `memory_items`/`memory_embeddings`；回滚可 `downgrade`，无其他 schema 依赖。

## 验证

- 契约：opt-in 强制、跨会话持久化、导出/删除、TTL 过期、低置信过滤、跨 tenant/subject 隔离、跨 namespace 写入拒绝。
- 安全：删除传播（正文清空 + 向量删除 + 缓存失效 + 脱敏审计）、投毒记忆不可信且不扩权、高敏感拒绝保存、跨租户读取拒绝。
- 工具：`save_memory`/`search_memory` 注册进 ToolRegistry，allowlist 与 wheel verifier 同步；CLI `memory view/delete/export` 走同一 policy 鉴权路径。

## 关联资料

- [Oria 架构设计](../../Oria架构设计.md)：Memory Protocol（§3.1）、RAG/记忆安全边界（§5.4 ADR-011/012/027）、memory-as-tool 工具清单（§5.2）。
- [Oria 详细执行路线](../Oria详细执行路线.md)：V0.5-T02 与 8.2/8.3 验证场景 S2/S3。
- [ADR-035：统一上下文预算与确定性事实账本](ADR-035-context-budget-and-fact-ledger.md)：T01 短期背景摘要决策。
