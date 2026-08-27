---
run_id: "20260827T232744+0800"
version: "V0.1"
task_id: "V0.1-T04"
depends_on: ["V0.1-T02-remediation-package-gate"]
verification_level: "F"
base_commit: "1b67b431e3ccf7def5400179a346c9ad816443b9"
commit: null
executed_at: "2026-08-27T23:27:44+08:00"
environment: "macOS 26.6.1 x86_64 / Python 3.11.15 / uv 0.12.6 / uv.lock sha256:bc4b7cea4ab80d6c3733680ffca96027e6fa3327bea3f4137f983d3a186a6c4a / httpx 0.28.1 / jsonschema 4.26.0 / pydantic 2.13.4 / pytest 9.1.1 / mypy 1.20.2"
provider_model: "DeepSeek deepseek-v4-flash profile（仅 MockTransport 契约，未调用真实 API）"
embedding_model: "BAAI/bge-small-zh-v1.5@e534609e6b53ac54bd42d8e87995d21a73b90bad（仅构造契约，未下载或推理）"
config_fingerprint: "sha256:1e7a80831392d6555d5c97dde70439345ebac16118681213f4acfba910ae08d1"
commands:
  - cmd: ".venv/bin/pytest -q tests/contract/test_t04_providers.py tests/unit/test_t04_embedders.py tests/security/test_t04_provider_boundaries.py"
    result: "21 passed；覆盖 10 条 Provider CT、4 条 Embedder UT、7 条安全边界"
  - cmd: "make lint"
    result: "70 files already formatted；Ruff All checks passed；mypy Success: no issues found in 47 source files"
  - cmd: "make test"
    result: "107 passed（not live / not enterprise / not performance）"
  - cmd: "make build"
    result: "Successfully built dist/oria-0.1.0.tar.gz 与 dist/oria-0.1.0-py3-none-any.whl"
  - cmd: "make smoke"
    result: "oria 0.1.0"
  - cmd: "临时隔离 venv 安装最终 wheel 后运行 scripts/verify_t02_wheel.py"
    result: "从 site-packages 导入 46 个 Oria 模块成功"
  - cmd: "临时隔离 venv 运行 scripts/verify_t03_wheel.py"
    result: "T03 wheel 资源、双 revision、幂等 data init 与零 Campaign/CouponBatch 表继续通过"
  - cmd: "临时隔离 venv 运行 scripts/verify_t04_wheel.py"
    result: "Mock/Fixture 唯一 Runtime 装配、Fixture 确定性、DeepSeek /responses + text.format 与本地结构化校验通过"
assertions:
  - id: "ADR-001 / Provider 归一化"
    covered: true
    note: "ADR 已实体化并接受；Oria 规范形、profile 级 dialect/structured mode、错误、reasoning/raw 与生命周期边界均由 adapter 契约实现。"
  - id: "V0.1-T04 / 唯一 Runtime 装配"
    covered: true
    note: "build_runtime() 按已解析 profile 装配 MockLLMProvider/OpenAICompatProvider 与 FixtureEmbedder/BGEEmbedder；HTTP client 进入同一 SealedAsyncExitStack，BGE 同步加载移入工作线程；未知实现 fail closed。"
  - id: "DeepSeek Responses native JSON Schema"
    covered: true
    note: "固定 POST /responses 与 text.format={type:json_schema,name,schema}；断言未发送 Chat Completions response_format；usage、tool call、assistant tool history 与结构化结果映射为 Oria 类型。"
  - id: "native / synthetic / unsupported"
    covered: true
    note: "三种 profile 策略均有 CT；保留工具由 adapter 截获，不进入业务 tool_calls；多个保留提交、保留提交与业务调用混合、schema 名与业务工具冲突均请求前或归一化期拒绝。"
  - id: "本地严格结构化校验"
    covered: true
    note: "ResponseSchema/ToolSpec 在构造期校验名称、保留名、对象根与 JSON Schema；结果先 JSON 解码再由 jsonschema 校验；strict=true 对嵌套 object 强制拒绝未知字段，即使输入 schema 显式允许 additionalProperties。"
  - id: "统一 stream / error"
    covered: true
    note: "DeepSeek 语义 SSE 映射 ReasoningDelta/TextDelta/ToolCallDelta/UsageDelta/Done；本地 sequence 单调；无终止事件与流失败产出 ProviderError；取消传播。401、429、timeout 与 5xx 使用安全错误类型，不回显上游正文。"
  - id: "reasoning / raw disclosure boundary"
    covered: true
    note: "ReasoningDelta 与 ChatResult.raw_response 继续默认排除公开序列化；内部 raw 上限 64 KiB，移除 reasoning，并按 credential 字段名脱敏。"
  - id: "Fixture / BGE Embedder"
    covered: true
    note: "Fixture 向量确定、有限、L2 归一化；BGE 构造要求非空固定 revision、禁止 remote code，encode 要求 normalization 并校验 batch、维度与有限值。BGE 测试使用注入 fake model，只证明 adapter 契约。"
  - id: "package 门禁"
    covered: true
    note: "CI package job 新增 verify_t04_wheel.py；本机最终 wheel 在全新 venv 的 site-packages 中执行 Provider/Embedder 行为验证成功。"
result: "passed"
blocked_by: []
known_limits:
  - "验证等级仅为 F。DeepSeek 使用 httpx.MockTransport，未发送真实网络请求、未取得真实 request ID，也不构成模型能力或质量证据。"
  - "未安装/下载/加载真实 BGE 模型，未执行真实本地 embedding 推理；固定 revision 来自公开模型仓库，Community Real 与 Live 结果仍待后续卡片。"
  - "V0.1 OpenAICompatProvider 当前只实现已要求的 DeepSeek Responses profile；Kimi、智谱、OpenAI 与 Chat Completions 方言仍未实现，Runtime 对未知实现明确失败。"
  - "V0.1 Core Gate 仍缺 T05–T09；Live 卡 T10 未运行，不能声明 V0.1 Core 或 Live 已通过。"
  - "GitHub Actions 尚未实际执行新增 verify_t04_wheel.py；本报告只记录本机 macOS 隔离 wheel 结果。"
  - "当前工作树同时包含尚未提交的 T03 remediation 01；本轮未创建 commit 或 push。"
---

# V0.1-T04 验证报告

## 结论

V0.1-T04 的 Provider、Embedder、结构化输出与唯一 Runtime 装配已完成，F 等级定向测试、全量 Core 门禁、构建、CLI smoke 和隔离 wheel 验证均通过，判定为 **passed**。

本报告没有把 fixture 冒充真实能力：DeepSeek 仅通过官方 Responses 语义构造的 MockTransport 契约验证，BGE 仅通过注入 fake model 验证固定 revision、禁用 remote code、归一化和维度边界。真实 DeepSeek/BGE 仍未运行。

## 关键实现

- 新增 `providers/` 边界层：Mock、OpenAI-compatible DeepSeek Responses、Fixture/BGE 与安全错误分类。
- DeepSeek 严格结构化输出只映射 `/responses` 的 `text.format`，不发送 Chat Completions `response_format=json_schema`。
- native、synthetic、unsupported 三种模式均显式配置；没有 prompt-only JSON fallback。
- 结构化结果在 adapter 内再次做本地严格校验，未知字段、非法 JSON/schema、多个保留提交和混合业务工具调用均失败。
- reasoning 与 provider raw 默认不进入公开投影；内部 raw 有界、去 reasoning、凭证字段脱敏。
- Provider/Embedder 只由 `build_runtime()` 装配和清理，没有新增第二套 Runtime。

## 外部契约核对

- DeepSeek `/responses`、`text.format` 与输出项依据 [Create Response](https://api-docs.deepseek.com/api/create-response/)；语义 SSE 事件依据 [Responses API Guide](https://api-docs.deepseek.com/guides/responses_api/)；HTTP 状态依据 [Error Codes](https://api-docs.deepseek.com/quick_start/error_codes/)。
- BGE adapter 的 `revision`、`trust_remote_code` 与归一化 encode 参数依据 [SentenceTransformer API](https://www.sbert.net/docs/package_reference/sentence_transformer/model.html)；模型 revision 固定为公开仓库提交 `e534609e6b53ac54bd42d8e87995d21a73b90bad`。

## 下一步

可进入 V0.1-T05：实现 ObjectStore、platform catalog、Chroma 投影、ingest/Retriever、citation 与 tenant-qualified CampaignRuleSnapshot。T05 必须继续区分 Fixture 与 Community Real 证据，并且只有在真实 BGE/Chroma 链路实际运行后才能记录 C 等级结果。
