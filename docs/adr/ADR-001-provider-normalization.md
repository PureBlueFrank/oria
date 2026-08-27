# ADR-001：Provider 归一化与显式能力矩阵

- 状态：已接受
- 日期：2026-08-27
- 决策者：Codex（V0.1-T04）
- 关联任务：V0.1-T04、V0.2-T01

## 背景

Oria 需要让同一 Agent/Graph 消费 Mock、OpenAI-compatible 与 Anthropic 模型，但不同厂商即使复用相似 API，也不会共享完全相同的 endpoint、流事件、reasoning、工具和结构化输出语义。若上层直接接收厂商 payload，业务流程会按 Provider 分叉；若把兼容性当作能力等价，又会把不支持的参数静默发送或在失败后猜测降级。

## 候选方案

1. 自建薄 Provider adapter：Oria 固定规范形值类型、标准错误和能力矩阵，各 adapter 只负责边界映射。代价是需要维护契约测试。
2. 直接依赖某一家 SDK 类型：实现快，但上层与厂商方言耦合，其他 Provider 会持续泄漏条件分支。
3. 引入聚合网关库：覆盖面广，但当前纵向切片无法证明具体 dialect、stream 与 structured-output 映射，也增加未使用依赖。

## 决策

采用方案 1：

1. `LLMProvider` 只接收/返回 Oria 的 `Message/ToolSpec/ChatOptions/ChatResult/StreamEvent`。
2. 每个 profile 固定 `api_dialect` 与 `structured_output_mode`；能力未知即 unsupported，请求前失败，不做隐式 JSON prompt fallback。
3. V0.1 的 DeepSeek profile 固定 Responses dialect，严格结构化输出映射到 `text.format={type: json_schema, name, schema}`。Chat Completions 的 `response_format=json_object` 不冒充 JSON Schema。
4. 所有结构化结果在 Provider 返回后继续执行本地 JSON Schema 校验；synthetic tool 使用保留名 `__oria_submit_response__`，不进入 ToolExecutor，且不得与业务工具调用混合。
5. reasoning 与 raw provider payload 只允许显式内部访问，默认 repr/序列化/日志投影排除。
6. 标准异常携带 `retryable/retry_after/provider_request_id`；Provider 不替 Agent/Tool 层执行重试，取消必须传播。
7. HTTP client 与真实模型资源只由唯一 `build_runtime()` 创建并纳入进程生命周期，不在 import、Context 或单次调用中建立全局资源。
8. FixtureEmbedder 用于确定性 CI；BGE 使用锁定 model revision 且 `trust_remote_code=false`，真实模型结果单独记录，不能由 Fixture 通过替代。

## 后果

- 正向影响：Graph 不按厂商分叉；dialect/能力偏差在请求前暴露；Mock 与真实 adapter 可以运行同一套契约测试。
- 代价与局限：新增 Provider 时必须实现请求、响应、错误、流式和结构化输出矩阵；静态 capability 只代表已验证 profile，不能外推到同厂商其他模型。
- 迁移/回滚：未来采用聚合网关时只能替换 adapter 内部实现，Oria 规范形和契约测试保持不变。

## 验证

- Mock 与 DeepSeek Responses adapter 产生相同内部规范形。
- `/responses` 与 `/chat/completions` payload 不混用，DeepSeek strict schema 只走 `text.format`。
- native、synthetic、unsupported、非法 JSON/schema、保留工具与业务工具混合均有 CT。
- reasoning/raw 默认投影不泄漏；401/429/5xx/timeout 映射为稳定错误且不暴露响应正文。
- Fixture 输出确定且有限；BGE revision/trust 边界和向量维度有 CT，真实加载另记 Live/Community 卡。

## 关联资料

- 架构主文档：§三.1、§四、§四.1、§四.3
- 详细执行任务：V0.1-T04、V01-LLM-01/02
- DeepSeek Responses API：<https://api-docs.deepseek.com/api/create-response/>
- DeepSeek Responses guide：<https://api-docs.deepseek.com/guides/responses_api/>
- DeepSeek error codes：<https://api-docs.deepseek.com/quick_start/error_codes/>
- Sentence Transformers API：<https://www.sbert.net/docs/package_reference/sentence_transformer/model.html>
