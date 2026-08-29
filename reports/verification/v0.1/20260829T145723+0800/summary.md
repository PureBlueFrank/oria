---
run_id: "20260829T145723+0800"
version: "V0.1"
task_id: "V0.1-T10"
milestone_id: null
depends_on: ["V0.1-T09", "V0.1-Core"]
verification_level: "C + L"
base_commit: "f6d052c0834ddf7addc254eee95387cc20e75254"
commit: null
worktree_state: "dirty；覆盖当前未提交的 V0.1-T07–T10 变更"
executed_at: "2026-08-29T14:57:23+08:00"
environment:
  os: "Darwin 26.6.1 x86_64"
  python: "3.11.15"
  uv: "0.12.6"
  install_mode: "source + locked standard extra"
provider_model:
  provider: "deepseek"
  api_dialect: "responses"
  model: "deepseek-v4-flash"
  revision: "DeepSeek-V4-Flash-0731（官方模型表；响应仅返回 alias）"
  request_ids:
    - "00e5a542-925a-4c02-9ffb-17f7a471ff4a"
    - "4dc7025b-954d-4fcc-8980-124c0c05e175"
    - "a9d4bd18-153a-4764-a910-20954110f2cf"
    - "f2d97f9d-8cf5-4847-bf5f-39deed8cc981"
    - "ab6aaf9d-12f3-462a-bbb6-5ade53890661"
    - "354fc1e3-f184-4347-883f-7dea5dc2112c"
embedding_model:
  provider: "sentence_transformers"
  model: "BAAI/bge-small-zh-v1.5"
  revision: "a7ec18349c42fc774b0e86af26215e38a10fbe9d"
  trust_remote_code: false
  license: "MIT"
  dimension: 512
config_fingerprint: "sha256:060d53ce79351ce10bb0e1cc3710178fcf409a4b2aeea0267c214ff3c0e855f4"
dataset_version: "scenario_a/1"
eval_fingerprint: null
commands:
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv sync --locked --group dev --extra standard"
    exit_code: 0
  - cmd: "锁定 BGE 首次联网推理；随后 HF_HUB_OFFLINE=1 离线复跑"
    exit_code: 0
  - cmd: "ORIA_RUN_LIVE=1 ORIA_LIVE_TARGETS=deepseek uv run pytest -m live -q（Key 由 macOS Keychain 临时注入）"
    exit_code: 0
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv run python scripts/run_scenario_a_golden.py"
    exit_code: 0
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache make lint"
    exit_code: 0
  - cmd: "UV_CACHE_DIR=.artifacts/uv-cache uv run pytest -m 'not live and not enterprise and not performance' -q"
    exit_code: 0
artifacts:
  - "reports/verification/v0.1/20260829T145723+0800/summary.md"
  - "reports/verification/v0.1/20260829T145723+0800/live-runs.json"
evidence_refs:
  - "reports/verification/v0.1/20260829T101609+0800/summary.md (V0.1-Core passed)"
  - "uv.lock (sha256:bc4b7cea4ab80d6c3733680ffca96027e6fa3327bea3f4137f983d3a186a6c4a)"
  - "eval/datasets/scenario_a/v1.manifest.json (sha256:3658665125a1b259cb6026a639c032657002fc16d51b92f099cc0479064e4529)"
  - "eval/baselines/scenario_a/1.json (sha256:76d369d18caf199eb34ba89a58a7c77f7501f66c9707b694793c5de51c176050)"
  - "eval/config/gates.yaml (sha256:8cec54ce608f55a2d7e4679cb965ff5ba3e67165d5e497df1a71075683c29cc8)"
result: "passed"
blocked_by: []
known_limits:
  - "Enterprise Adapter 未运行、未验证。"
  - "远端 GitHub Actions 未在当前未提交 worktree 上运行。"
  - "报告中的 token usage 是成功 ChatResult 的聚合；每个 workflow 有一次被本地结构化校验拒绝的 Provider 响应，其 usage/request ID 未穿过现有错误契约。"
  - "total_cost 未计算；本次仅记录 Provider 返回的 token usage，不使用未冻结价格倒推费用。"
---

# V0.1-T10 DeepSeek + BGE Live 验证卡

## 结论

`V0.1-T10` 本次结果为 **passed**。真实 `deepseek-v4-flash` Responses 请求、锁定 BGE、本地 Chroma/SQLite 和正式 Graph/Tool 链路完成两次连续运行。两次结果均通过 `CampaignProposal` schema、可信证据、引用可回查、硬资格候选子集和业务零副作用校验；第二次数据初始化新增商家为 0，知识摄取为幂等。

允许声明：“V0.1 社区版真实模型 MVP 已验证（DeepSeek + 锁定本地 BGE）”。这不代表其他 Provider、企业 Adapter、真实客户数据或生产规模已经验证。

## 实际结果

| 项目 | 第一次 | 第二次 |
| --- | ---: | ---: |
| Provider 成功 request ID | 3 | 3 |
| 成功响应 input/output tokens | 11,937 / 1,652 | 12,037 / 1,471 |
| 正式只读工具调用 | 2 | 2 |
| 硬资格候选 / 推荐商家 | 10 / 10 | 10 / 10 |
| 六类规则 / 可回查引用 | 6 / 52 | 6 / 52 |
| 新增种子商家 | 12 | 0 |
| 知识摄取幂等 | false | true |
| Campaign/Coupon 业务表 | 0 | 0 |

DeepSeek 的模型与 Responses 能力以 [官方 Responses API](https://api-docs.deepseek.com/api/create-response/) 和 [官方模型表](https://api-docs.deepseek.com/quick_start/pricing/) 为依据。BGE revision `a7ec18349c42fc774b0e86af26215e38a10fbe9d` 包含 safetensors，模型卡标记 MIT：[BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5/tree/a7ec18349c42fc774b0e86af26215e38a10fbe9d)。

## T10 真实运行发现并修复的问题

1. 原锁定 BGE revision 不含 safetensors，`torch 2.2.2` 因 CVE-2025-32434 安全门禁拒绝加载 pickle 权重；revision 前移到同时包含模型更新和 safetensors 的不可变提交，没有绕过安全检查。
2. Provider parser 曾把“纯业务工具调用”误判为结构化结果与工具混合；现允许纯工具轮，仍拒绝真实混合输出。
3. `httpx` 默认 5 秒导致最终轮超时；DeepSeek 客户端现显式使用 120 秒总超时、10 秒连接超时，并在 checkpoint 保存脱敏错误码。
4. 让 LLM 机械回写六类规则、预览和 52 条权威引用既耗费 token 又不可靠；Provider 输出已收窄为商家软排序草案，最终规则、预览与引用全部由可信 Tool 结果本地确定性组装。伪造候选和额外权威字段继续 fail closed，`sa-v1-024` 与 Golden 30/30 均通过。

## 验证命令与断言

- `pytest -m live`：`1 passed, 180 deselected`，双跑耗时 62.19 秒。
- Core：`180 passed, 1 deselected`。
- Golden：`30/30`，五项指标均为 `1.0`。
- Ruff format/check 与 mypy：通过。
- BGE 首次下载后 `HF_HUB_OFFLINE=1` 复跑：512 维，两个向量 norm 均为 `1.0`，无远程代码。
- 两次业务库表均仅为 `alembic_version_business`、`merchants`；无 Campaign/CouponBatch 表或写入。
- Key 只由 macOS Keychain 注入测试子进程；报告、Git diff 与运行 JSON 均不保存 Key。

## 已知限制

本报告没有验证其他 Provider、企业 Adapter、真实客户数据、生产流量或远端 CI。Provider 当前只聚合成功 `ChatResult` 的 token usage；本地拒绝的结构化响应可能仍在上游计费，因此不得用本报告的 token 合计推导精确账单。
