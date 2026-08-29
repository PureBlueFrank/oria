---
run_id: "20260829T104059+0800"
version: "V0.1"
task_id: "V0.1-T10"
milestone_id: null
depends_on: ["V0.1-T09", "V0.1-Core"]
verification_level: "C + L preflight"
base_commit: "f6d052c0834ddf7addc254eee95387cc20e75254"
commit: null
worktree_state: "dirty；预检覆盖当前未提交的 V0.1-T07–T10 变更"
executed_at: "2026-08-29T10:40:59+08:00"
environment:
  os: "Darwin 25.6.0 x86_64"
  python: "3.11.15（锁定虚拟环境）"
  uv: "0.12.6"
  install_mode: "source preflight; Live run not started"
provider_model:
  provider: "deepseek"
  api_dialect: "responses"
  model: "deepseek-v4-flash（计划目标，未请求）"
  revision: null
  request_ids: []
embedding_model:
  provider: "sentence_transformers"
  model: "BAAI/bge-small-zh-v1.5（计划目标，未加载）"
  revision: "e534609e6b53ac54bd42d8e87995d21a73b90bad"
  trust_remote_code: false
  license: "not observed; model artifact/model card not present in the execution environment"
config_fingerprint: null
dataset_version: "scenario_a/1"
eval_fingerprint: null
commands:
  - cmd: "只检查 DEEPSEEK_API_KEY 是否为非空，不输出值"
    exit_code: 0
    result: "deepseek_key=missing"
  - cmd: ".venv/bin/python -c '<importlib presence preflight>'"
    exit_code: 0
    result: "sentence_transformers=missing; torch=missing"
  - cmd: "检查锁定 BGE 的本地 Hugging Face cache 目录是否存在"
    exit_code: 0
    result: "bge_cache=missing"
  - cmd: "ORIA_RUNTIME_PROFILE=standard ORIA_LLM_PROFILE=deepseek ORIA_EMBEDDING_PROFILE=bge ORIA_DATA_DIR=.artifacts/v01-t10-preflight UV_CACHE_DIR=.artifacts/uv-cache uv run oria config doctor --output json"
    exit_code: 2
    result: "invalid_config: active profile requires environment variable DEEPSEEK_API_KEY"
artifacts:
  - "reports/verification/v0.1/20260829T104059+0800/summary.md"
evidence_refs:
  - "reports/verification/v0.1/20260829T101609+0800/summary.md (V0.1-Core passed)"
  - "uv.lock (sha256:bc4b7cea4ab80d6c3733680ffca96027e6fa3327bea3f4137f983d3a186a6c4a)"
  - "eval/datasets/scenario_a/v1.manifest.json (sha256:3658665125a1b259cb6026a639c032657002fc16d51b92f099cc0479064e4529)"
  - "eval/baselines/scenario_a/1.json (sha256:76d369d18caf199eb34ba89a58a7c77f7501f66c9707b694793c5de51c176050)"
  - "eval/config/gates.yaml (sha256:8cec54ce608f55a2d7e4679cb965ff5ba3e67165d5e497df1a71075683c29cc8)"
assertions:
  - id: "live-target-selection"
    covered: true
    note: "显式选择 standard/deepseek/bge，没有回退至 MockLLM 或 FixtureEmbedder。"
  - id: "secret-fail-closed"
    covered: true
    note: "DeepSeek 凭证缺失时 config doctor 以退出码 2 在网络请求前拒绝，未输出或记录密钥值。"
  - id: "pinned-bge-precondition"
    covered: true
    note: "配置锁定 BAAI/bge-small-zh-v1.5 revision e534609e6b53ac54bd42d8e87995d21a73b90bad 且 trust_remote_code=false，但当前依赖和模型缓存均不存在。"
  - id: "real-deepseek-request"
    covered: false
    note: "未发起请求；无 request ID 或 usage。"
  - id: "real-bge-inference"
    covered: false
    note: "未安装 sentence-transformers/torch，未下载或加载锁定 BGE revision。"
  - id: "scenario-a-s2"
    covered: false
    note: "因必需前置缺失，没有进入 Runtime、Graph、Tool 或业务数据步骤。"
result: "blocked"
blocked_by:
  - "DEEPSEEK_API_KEY 未配置；DeepSeek 请求在发送前被正确拒绝。"
  - "standard optional dependencies 未安装：sentence-transformers 与 torch 均不存在。"
  - "锁定 BGE revision 没有本地缓存，本次未执行首次模型下载。"
known_limits:
  - "没有真实 DeepSeek model/revision/request ID/usage/成本/延迟证据。"
  - "没有真实 BGE 加载、推理、license 或首次下载/后续离线对照证据。"
  - "本报告只能证明 Live 前置门禁 fail closed，不能证明 S2 或真实模型 MVP 通过。"
---

# V0.1-T10 DeepSeek + BGE Live 验证卡

## 结论

`V0.1-T10` 本次结果为 **blocked**。已显式选择 `runtime_profile=standard`、DeepSeek Responses profile 和锁定 BGE profile，但执行环境没有 `DEEPSEEK_API_KEY`，也没有 standard 依赖或 BGE 模型缓存。Oria 在任何网络请求、模型加载、Graph 或 Tool 执行前以退出码 2 拒绝启动。

这不是 DeepSeek 或 BGE 的运行失败，而是 Live 前置未满足。`V0.1-Core` 的 passed 结论保持不变；V0.1 当前状态为“Core 已通过，Live blocked”，不得声称“V0.1 全部通过”或“真实模型 MVP 已验证”。

## 恢复条件

1. 在本地执行环境安全设置非空 `DEEPSEEK_API_KEY`，不把值写入仓库、报告或聊天。
2. 允许按 `uv.lock` 安装 `standard` extra，并下载锁定的 `BAAI/bge-small-zh-v1.5@e534609e6b53ac54bd42d8e87995d21a73b90bad`。
3. 以新 run ID 重新执行 S2；将真实 request ID、usage、BGE model/revision/license、引用/资格/副作用断言和首次下载/离线对照记入新报告。
