---
run_id: "20260828T005408+0800"
version: "V0.1"
task_id: "V0.1-T04-remediation-01+V0.1-T05"
depends_on: ["V0.1-T03-remediation-01", "V0.1-T04"]
verification_level: "F"
base_commit: "1b67b431e3ccf7def5400179a346c9ad816443b9"
commit: null
executed_at: "2026-08-28T00:54:08+08:00"
environment: "macOS 26.6.1 x86_64 / Python 3.11.15 / uv 0.12.6 / uv.lock sha256:bc4b7cea4ab80d6c3733680ffca96027e6fa3327bea3f4137f983d3a186a6c4a / Chroma 1.5.9 / httpx 0.28.1 / jsonschema 4.26.0 / pydantic 2.13.4 / pytest 9.1.1 / mypy 1.20.2"
provider_model: "DeepSeek deepseek-v4-flash profile（仅 MockTransport 契约，未调用真实 API）"
embedding_model: "FixtureEmbedder 128 维；BGE 仅 fake adapter 验证，未下载/推理真实模型"
config_fingerprint: "runtime-specific; no secret persisted"
commands:
  - cmd: "make lint"
    result: "84 files formatted；Ruff All checks passed；mypy Success: no issues found in 57 source files"
  - cmd: "make test"
    result: "138 passed（not live / not enterprise / not performance）"
  - cmd: "make build"
    result: "Successfully built dist/oria-0.1.0.tar.gz 与 dist/oria-0.1.0-py3-none-any.whl"
  - cmd: "make smoke"
    result: "oria 0.1.0"
  - cmd: "隔离 venv 安装最终 wheel 并运行 verify_t02_wheel.py–verify_t05_wheel.py"
    result: "从 site-packages 导入 56 个 Oria 模块；T02/T03/T04/T05 安装包验证全部通过"
assertions:
  - id: "T04 / 流式结构化输出"
    covered: true
    note: "native/synthetic 流在终止前缓冲并执行本地 schema 校验；保留提交不作为业务 ToolCallDelta 暴露，与业务工具混合失败。"
  - id: "T04 / Mock、BGE 与诊断边界"
    covered: true
    note: "Mock 支持常用严格 minLength/minItems/minimum 与本地 ref/choice witness；BGE 拒绝非单位向量；apiKey/client_secret/access_token 等键变体脱敏。"
  - id: "V01-RAG-01/02"
    covered: true
    note: "全新临时 data_dir 中真实运行 SQLite/ObjectStore/Chroma；10 个固定 Fixture 问题实测 Recall@3=10/10，citation 的 document/version/chunk 可回查。"
  - id: "V01-RULE-01/02"
    covered: true
    note: "六类规则含商品圈选、招后选品、金额/折扣/阶梯与列表叶子 citation；缺失/冲突/非法/失效拒绝；黑白名单与销售组织原文不进入 Retriever 公开内容或 snapshot 公开序列化。"
  - id: "T05 / 投影生命周期"
    covered: true
    note: "Chroma collection 按 embedding provider/model/revision/dimension 指纹隔离；128→32 维 profile 切换可复用 catalog 并重建；catalog/object content hash 篡改失败；删除传播使快照 citation 失效。"
  - id: "T05 / tenant、ACL 与投影污染"
    covered: true
    note: "tenant 进入 chunk ID；Chroma 前置 tenant/ACL filter 与 catalog/ObjectStore 后置回源同时生效；跨 tenant 相同文档不覆盖，Chroma 文本被篡改时 Retriever 返回 ObjectStore 真值。"
  - id: "package 门禁"
    covered: true
    note: "CI package job 新增 verify_t05_wheel.py；本机最终 wheel 在隔离 venv 从 site-packages 执行 ingest/retrieve/snapshot/rebuild/delete 通过。"
result: "passed"
blocked_by: []
known_limits:
  - "验证等级仅为 F；Recall@3 是合成 Fixture + FixtureEmbedder 结果，不是真实 BGE 或业务质量证据。"
  - "DeepSeek 仍仅 MockTransport，未发送真实请求，未获取 provider request ID/usage。"
  - "未安装/加载真实 BGE，Community Real 与 Live 卡未运行，不得声称 C/L 等级通过。"
  - "V0.1 Core Gate 仍缺 T06–T09，Live 卡 T10 未运行。"
  - "GitHub Actions 未在远端实际执行新增 T05 package step；本报告只记录本机 macOS 结果。"
  - "当前工作树同时含 T03 remediation、T04/T05 未提交变更；本轮未创建 commit 或 push。"
---

# V0.1-T04 remediation 01 + T05 验证报告

## 结论

T04 审查发现的流式结构化输出、Mock schema witness、BGE 归一化和诊断脱敏问题已修复；T05 的规则字段、叶子 citation、embedding profile 投影隔离、tenant/ACL、ObjectStore 回源、删除/重建与快照完整性已完成 F 等级验证，判定为 **passed**。

本结论不包含真实 DeepSeek/BGE；本地 Chroma 是真实组件，但 embedding 仍为 Fixture。因此不构成 Community Real 或 Live 通过声明。

## 主要修复

- 流式 native/synthetic 结构化输出在暴露前先完成本地校验；保留工具和业务工具混合直接失败。
- Mock 可为常用严格 JSON Schema 生成确定性 witness；BGE 返回值必须是有限的单位向量。
- 报名规则补齐客户圈选、商品价格/类目/关键词和招后选品策略、版本、模式与完成条件。
- citation 递归到列表中的金额、阶梯阈值与出资金额叶子。
- Chroma collection 按 embedding projection 指纹与维度隔离，文档原文/catalog 不再与某一投影 profile 绑死。
- 新增 T05 安装 wheel 验证脚本与 CI package step。

## 下一步

可进入 V0.1-T06，实现 `search_campaign_rules`、`query_merchants` 与 ToolRegistry。真实 DeepSeek+BGE 仍必须在独立 Community Real/Live 卡中执行和记录。
