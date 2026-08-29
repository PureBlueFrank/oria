---
run_id: "20260829T214048+0800"
version: "V0.2"
task_id: "V0.2-T03"
depends_on: ["V0.2-T02", "V0.1-T05"]
verification_level: "Fixture + Community CT/IT/SEC"
base_commit: "1e364bc9d99ebdee67bf36cba7b9793d5de1fe91"
commit: null
worktree_state: "dirty；仅含 V0.2-T03 阶段 D 的 S2 测试、路线状态与本报告"
executed_at: "2026-08-29T21:40:48+08:00"
environment:
  os: "Darwin 25.6.0 x86_64"
  python: "3.11.15"
  uv: "0.12.6"
  install_mode: "source + isolated installed wheel"
provider_model:
  provider: null
  model: null
  revision: null
  request_ids: []
config_fingerprint: "null；测试均使用临时 data_dir 与确定性默认配置"
dataset_version: "scenario_a/v1"
eval_fingerprint: "sha256:365cddf107f9bc1e84b7cdeeeaddb8d08db784df533c18c4a0b8332ff7e2512e"
result: "passed"
blocked_by: []
known_limits:
  - "未运行真实 Provider/模型请求、Live、Enterprise 或 Performance；不得据此声明相关能力已验证。"
  - "本次检索与生命周期证据使用本地 SQLite、Chroma 与 FixtureEmbedder，不是生产规模或多进程并发验证。"
  - "V0.2 Core 尚需 V0.2-T04–T05 和真实 BGE 对照；本报告只收口 V0.2-T03。"
---

# V0.2-T03 AuthorizedRetriever 与知识生命周期验证卡

## 1. 结论

- 实际结果：`passed`。
- 允许声明：V0.2-T03 的本地 Community 版本策略、AuthorizedRetriever、更新/删除传播、citation 校验与 catalog→vector rebuild 门禁已通过。
- 禁止声明：不能推导真实 Embedding/Provider、企业 Adapter、生产并发或 Performance 已验证，也不能声明 V0.2 Core 已完成。
- 下一门禁：V0.2-T04 检索管线、V0.2-T05 RAG eval 与真实 BGE 对照。

## 2. 环境与身份

| 项目 | 实际值 | 取证方式 |
| --- | --- | --- |
| OS / architecture | Darwin 25.6.0 / x86_64 | `uname -srvmp` |
| Python / uv | Python 3.11.15 / uv 0.12.6 | `uv run python --version` / `uv --version` |
| base commit / worktree | `1e364bc9d99ebdee67bf36cba7b9793d5de1fe91`；阶段 D 收口文件未提交 | `git rev-parse HEAD` / `git status --short` |
| `uv.lock` hash | `sha256:bc4b7cea4ab80d6c3733680ffca96027e6fa3327bea3f4137f983d3a186a6c4a` | `shasum -a 256 uv.lock` |
| migration manifest | `sha256:10cc961c06e83042cb1ada41ea90851410c367b7396b9b6c206f1fc670f8bc66` | `shasum -a 256 src/oria/migrations/manifest.json` |
| Scenario A dataset | `v1`，manifest `sha256:3658665125a1b259cb6026a639c032657002fc16d51b92f099cc0479064e4529` | `shasum -a 256 eval/datasets/scenario_a/v1.manifest.json` |
| Provider / model / request ID | 未运行 / 无 / 无 | 本任务禁止 Live |

## 3. 实际执行命令

| # | 可复制命令 | 退出码 | 结果摘要 |
| --- | --- | --- | --- |
| 1 | `uv run pytest -m "not live and not enterprise and not performance" -q` | 0 | `277 passed, 1 deselected in 49.16s` |
| 2 | `uv run pytest -m security -q` | 0 | `49 passed, 229 deselected in 7.23s` |
| 3 | `uv run ruff check . && uv run ruff format --check . && uv run mypy src/oria` | 0 | `All checks passed`; `133 files already formatted`; `Success: no issues found in 80 source files` |
| 4 | `uv build` | 0 | 构建 `oria-0.1.0.tar.gz` 与 `oria-0.1.0-py3-none-any.whl` |
| 5 | `uv venv /tmp/oria-v02-t03-wheel.soxT6N/venv --python 3.11 && uv pip install --python /tmp/oria-v02-t03-wheel.soxT6N/venv/bin/python dist/oria-0.1.0-py3-none-any.whl && /tmp/oria-v02-t03-wheel.soxT6N/venv/bin/python scripts/verify_t03_wheel.py --data-dir /tmp/oria-v02-t03-wheel.soxT6N/data` | 0 | 已安装 wheel 的 `platform_0004`、双库幂等 init 与资源校验通过 |
| 6 | `uv run python scripts/run_scenario_a_golden.py` | 0 | `30/30`；五项冻结指标均为 `1.0` |

wheel SHA-256：`77437a730240ce97959cd1b7adf34e67122230fe0c03e50d725b52dd6bd9d5ce`。Golden 输出 `.artifacts/eval/scenario_a_v1.json` SHA-256：`365cddf107f9bc1e84b7cdeeeaddb8d08db784df533c18c4a0b8332ff7e2512e`。两者为 gitignore artifact，持久化脱敏摘要为本报告。

## 4. 断言矩阵

| ID | 可观察断言 | 直接路径 | 绕过 / 失败路径 | 结果 |
| --- | --- | --- | --- | --- |
| MIG-04 | 空库升级至 `platform_0004` 并可回滚至 `platform_0003` | 新列存在，head 正确 | 旧数据 owner/classification 回填，旧 completed version 被 supersede | `passed` |
| VER-01 | content/ACL/metadata/owner/classification 按 version 不可变 | 新 version 成为唯一 active | 同 version 修改 owner 被拒绝；superseded version 不可重激活 | `passed` |
| V02-RAG-02 | 调用方不能去除 tenant/ACL filter | PolicyDecision→ACLFilter 的 Chroma pre-filter + catalog post-filter | 传入伪造 `tenant_id` reserved filter 直接拒绝 | `passed` |
| V02-RAG-03 | 恶意指令文档只是引用数据 | `Doc.trust_level == "untrusted_data"` | 文档中的 `persist_campaign` 指令不能改动 sealed Tool allowlist，执行被拒绝 | `passed` |
| V02-RAG-04 | 更新/删除传播到 chunk/vector/citation | 新 version 返回 6 chunks，旧 version 跨 projection 清理 | 注入清理失败后 catalog 仍拒绝旧引用，幂等重试完成清理 | `passed` |
| CIT-01 | citation 必须匹配当前 version/chunk/content hash | 当前引用可回源 ObjectStore | superseded/删除引用、向量元数据不匹配、ObjectStore 篡改均 fail closed | `passed` |
| REBUILD-01 | rebuild 仅恢复仍被引用的版本 | catalog 1 active version 对应 6 chunks | 被删 tenant 重建结果为 `(0 versions, 0 chunks)` | `passed` |
| V0.2-S2 | 双 tenant + 不同 ACL 生命周期闭环 | 分别查询、A 更新、B 删除、分别重建 | 同 document ID 不跨 tenant 召回，A 旧 citation 与 B 已删 citation 失效 | `passed` |

## 5. 验证等级分离

| 卡片 | 本次状态 | 证据边界 |
| --- | --- | --- |
| Fixture | `passed` | FixtureEmbedder、合成文档与故障注入 |
| Community | `passed` | 本地真实 SQLite、ObjectStore、Chroma，源码与隔离 wheel |
| Live | `not-run` | 无 Provider/模型请求，无 request ID |
| Enterprise / E-like | `not-run` | 无企业 Adapter 或真实客户数据 |
| Performance | `not-run` | 按任务边界显式排除 |

## 6. 安全、数据与副作用

- 信任边界：PolicyDecision 是 ACLFilter 唯一来源；调用方 query filter 不能覆盖策略；检索文本始终是 `untrusted_data`。
- 数据：仅使用仓库已声明的合成、脱敏 Fixture，未读取密钥、`.env`、真实客户或商家数据。
- 副作用：仅修改临时 test data_dir、gitignore 的 build/eval artifact 与 `/tmp/oria-v02-t03-wheel.soxT6N`；未执行外部写入、push 或生产变更。
- 脱敏：报告只记录合成 tenant/document 标识、命令摘要与哈希，不含凭证、原始提示词或 PII。

## 7. Artifacts 与可复现性

- 持久化报告：`reports/verification/v0.2/20260829T214048+0800/summary.md`
- 迁移 manifest：`src/oria/migrations/manifest.json`
- 已忽略构建产物：`dist/oria-0.1.0-py3-none-any.whl`
- 已忽略 Golden 原始输出：`.artifacts/eval/scenario_a_v1.json`

## 8. 已知限制与后续

- 未执行：Live、Enterprise、Performance、真实 BGE 对照与远程 GitHub Actions。
- 失败项：无。
- `blocked_by`：无（仅就 V0.2-T03 范围）。
- 剩余风险：本地单进程 fixture 不证明生产规模并发、远程向量库或备份保留策略。
- 后续：进入 V0.2-T04；V0.2 Core 仍按路线等待 T04–T05 及对应真实 BGE 门禁。
