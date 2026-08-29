---
run_id: "20260829T093711+0800"
version: "V0.1"
task_id: "V0.1-T08"
depends_on: ["V0.1-T07"]
verification_level: "F"
base_commit: "f6d052c0834ddf7addc254eee95387cc20e75254"
commit: null
executed_at: "2026-08-29T09:37:11+08:00"
provider_model: "DemoMockLLMProvider（无状态离线 fixture）；未调用真实 Provider"
embedding_model: "FixtureEmbedder；未下载或推理真实 BGE"
commands:
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv run pytest tests/contract/test_context_isolation.py tests/contract/test_t04_providers.py tests/integration/test_t08_demo.py -q"
    result: "19 passed；覆盖 correlation 隔离、typed dynamic map schema、同 Runtime 双 run、CLI 断网双跑、失败 unwind"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache make lint"
    result: "120 files formatted；Ruff All checks passed；mypy Success: no issues found in 76 source files"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache make test"
    result: "178 passed（not live / not enterprise / not performance）"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache make build"
    result: "sandbox 内因 PyPI DNS 受限失败；按权限流程联网重试后成功构建 wheel 与 sdist"
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache make smoke"
    result: "oria 0.1.0"
  - cmd: "将 wheel 以 --no-deps 安装到隔离 site-packages，在无源码 cwd 运行 scripts/verify_t08_wheel.py"
    result: "已安装 wheel 从全新目录离线运行两次；10 商家、2 工具事件、报告落盘、幂等初始化与零业务副作用全部通过"
  - cmd: "首次尝试在空 venv 中 --no-deps 安装 wheel"
    result: "验证环境在导入 Oria 前即因缺 pydantic 失败，不是 Demo 执行结果；改用锁定开发环境依赖 + 隔离 wheel-only site-packages 后完成有效门禁"
assertions:
  - id: "unique runtime assembly"
    covered: true
    note: "build_runtime 装配 DB/Repository/Saver/Vector/ObjectStore/Provider/Tool 并注册已编译 research_agent；开始失败沿既有 SealedAsyncExitStack 逆序 unwind。"
  - id: "per-run context and correlation"
    covered: true
    note: "同 Runtime 两次 execute_demo 的 session/thread/run/correlation 全部不同；无状态 Demo Mock 不持有 run metadata，每条 Console JSON 事件均可回查本次 run/correlation。"
  - id: "automatic initialization and repeatability"
    covered: true
    note: "第一次播种 12 家 fixture 商家并建立 SQLite/Chroma/规则文档；第二次 merchants_inserted=0、ingestion.idempotent=true。"
  - id: "CampaignProposal validation"
    covered: true
    note: "输出经严格 ResponseSchema、trusted rule/merchant evidence、引用回查、六类规则和候选子集校验；修复 strict schema 对 typed dynamic map 的误判。"
  - id: "business side-effect freedom"
    covered: true
    note: "提案包含 10 家硬资格商家，demo-m003/demo-m004 未进入结果；business DB 只有 alembic_version_business/merchants，无 Campaign/CouponBatch 表或写入。"
  - id: "source and installed-wheel offline CLI"
    covered: true
    note: "源码 CLI 在 socket.connect 被强制拒绝时双跑通过；已安装 wheel 的子进程 cwd 不含源码，且未提供 Oria 配置或云凭证。"
result: "passed"
blocked_by: []
known_limits:
  - "GitHub Actions 尚未在本次未提交变更上远端实跑；不将本地结果写成远端 CI 通过。"
  - "验证等级为 F；真实 DeepSeek/BGE 仍未运行，不构成 Community Real 或 Live 证据。"
  - "当前 T07/T08 变更均未提交，故 commit 保持 null。"
---

# V0.1-T08 验证报告

## 结论

V0.1-T08 已完成 F 等级收口。源码态与已安装 wheel 均可在全新目录零配置离线运行 `oria demo --output json`，重复运行保持初始化幂等，且没有 Campaign/CouponBatch 副作用。全量 Core 为 178 passed，wheel/sdist、CLI smoke 和已安装 wheel 门禁通过。

V0.1 版本仍为进行中：T09 文档/威胁模型/Core report 尚未完成，T10 真实 DeepSeek+BGE Live 卡未运行。

## 安全与真实性边界

- Demo 仅使用离线 MockLLM + FixtureEmbedder，不声称真实模型质量。
- 黑名单 `demo-m004` 与非营业 `demo-m003` 均由 EligibilityPolicy 确定性排除，LLM 不能改写硬资格。
- Demo 只注册 `search_campaign_rules` / `query_merchants` 两个只读工具，最终只产出预览与验证报告，不持久化活动或券批次。
