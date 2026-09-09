# ADR-035：统一上下文预算与确定性事实账本

- 状态：已接受
- 日期：2026-09-10
- 决策者：FrankLee
- 关联任务：V0.5-T01

## 背景

`research_agent` 已有累计模型轮数、输入/输出 Token、成本和工具调用的硬终止限制，但原实现会把 checkpoint 中的全部 `messages` 直接交给 Provider。随会话变长，单次模型输入可能先于累计硬预算溢出。只保留自由文本摘要又无法对压缩前后的关键事实做逐项机器断言。

T01 仅治理会话级短期历史。长期跨会话记忆的 opt-in、tenant/subject 命名空间、TTL、查看/删除/导出和 memory-as-tool 都属于 T02。

## 候选方案

1. 仅使用 Provider 返回的 Token 用量：计量精确，但只能在请求后获得，无法阻止本次输入溢出。
2. 调用 LLM 做摘要和事实判断：表达更自然，但增加不确定性、成本、循环失败点和事实篡改风险。
3. 在进入 Provider 前使用确定性估算与滑窗压缩，并把结构化事实账本与压缩后历史一起写入 checkpoint state。

## 决策

采用方案 3：

- `ContextBudget` 同时定义上下文上限和预留余量。社区版默认上限为 32,000，为输出与临时收尾指令预留 8,000；数值是具名常量，不散落在压缩逻辑中。
- Token 估算是纯函数：消息内容按 UTF-8 字节数除以可配系数并向上取整，再加每消息固定开销。它是进入 Provider 前的保守窗口门禁，不代替 Provider usage 记账。
- 压缩保留所有 system 消息和预算内最近的连续消息后缀；更早内容被替换为一条规范 JSON 摘要占位。如保留的 system 消息本身超预算，压缩不会截断或改写高权限指令；该配置错误应由上层拒绝，不伪造“已在预算内”。
- 事实提取仅读取 assistant/tool 消息中可解析 JSON 的具名标量字段，例如 ID、名称、金额、计数、状态、结论和根因。顺序和去重规则固定，不调用 LLM。
- 事实账本不保存完整 prompt，排除凭证/令牌/授权/prompt/content 类字段、电邮和电话形态，并限制单个字符串事实长度。账本是模型生成历史的可回查投影，不是权威业务事实源，不得覆盖业务系统读取。
- `research_model_node` 仍先执行 `ResearchLimits` 剩余轮数/Token/成本/截止时间检查，再对本次模型消息做 context 压缩。累计硬终止不会因压缩重置或放宽。
- `InMemoryMemory` 仅按 tenant + session 隔离进程内短期历史。`search()` 在 T01 固定返回空列表；不提供 `save/search_memory` 工具，不新增数据库 migration。

## 后果

- 正向影响：单次模型输入在 Provider 调用前有统一可测量门禁；关键事实可逐项断言；压缩路径可离线重放。
- 代价与局限：启发式估算不等于特定 Provider tokenizer；确定性摘要只保留明示结构化事实，不保证保留早期自由文本的修辞与推理过程；进程重启后仅依赖 checkpoint state 恢复。
- 迁移/回滚：新 `fact_ledger` 是 `NotRequired` 且所有读取均有空值默认，可读取旧 checkpoint。回滚可停止 runtime 挂载和 model 构造处压缩，无数据库 schema 回滚。

## 验证

- 确定性 Token 估算的空列表、ASCII、UTF-8 多字节、参数边界。
- 预算内不压缩，溢出时压缩，压缩后估算值不超过消息预算。
- 商家 ID、商家名称、金额和结论在压缩前后逐项匹配。
- `Memory` 的 append/load/compress/search 契约、tenant + session 隔离、JSON 序列化和 agent loop 接入回归。

## 关联资料

- [Oria 架构设计](../../Oria架构设计.md)：Memory Protocol、Agent loop 预算与记忆安全边界。
- [Oria 详细执行路线](../Oria详细执行路线.md)：V0.5-T01 与 8.3 核心测试。
- [ADR-012：Memory 生命周期主题](README.md)：T02 前仍为计划中。
- [ADR-027：RAG/Memory 内容安全主题](README.md)：T02/T03 前仍为计划中。
