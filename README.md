# Oria

Oria 是面向招商活动编排的 AI Agent 平台。当前 V0.1 交付了一条会永久保留的社区版只读纵向切片：它使用真实本地 SQLite、Chroma 和 ObjectStore，由确定性 Mock LLM 驱动正式 Graph 与 Tool 契约，生成可回查证据的招商活动提案。

## 五分钟运行离线 Demo

需要 Python 3.11 和 uv 0.12.6。首次运行会在当前目录下自动初始化 `.oria-data`；也可以显式指定数据目录。

```bash
uv sync --locked --group dev
uv run oria demo --output json
uv run oria demo --output json --data-dir /tmp/oria-demo-data
```

Demo 全程无网络依赖，重复执行是幂等的。JSON 输出包含：

- `search_campaign_rules` 和 `query_merchants` 两类正式只读 Tool 事件；
- 六类活动规则及可回查引用；
- 10 家符合硬资格的 Fixture 商家，以及预览、理由和未决项；
- `run_id`/correlation、usage、schema/引用/候选子集/业务零副作用校验结果。

演示不创建 `Campaign` 或 `CouponBatch`，不连接企业系统，也不会把黑名单或非正常营业商家塞入提案。每次运行的脱敏证据保存在数据目录的 `reports-tmp/`。

## 开发与验证

```bash
uv run oria config doctor --output json
uv run oria data init --output json
make lint
make test
uv run python scripts/run_scenario_a_golden.py
make build
make smoke
```

Golden 评测是 30 条已人工批准的离线场景 A 样本；安全、schema、引用、工具和关键样本指标要求 100%，且不得相对冻结 baseline 回归。

## 验证状态与声明边界

| 能力 / 验证层级 | 当前状态 |
| --- | --- |
| Community 离线 Demo / V0.1 Core | 188 项本地 Core 门禁通过；详见 [V0.1 审计修复报告](reports/verification/v0.1/20260829T160420+0800/summary.md) |
| 场景 A Golden | 30/30 已批准，确定性门禁通过 |
| DeepSeek Responses Adapter | 真实 `deepseek-v4-flash` Responses 双跑通过 |
| BGE Embedder | 锁定 revision 的 safetensors 首次加载与离线复跑通过 |
| V0.1-T10 DeepSeek + BGE Live | `passed`；详见 [T10 Live 报告](reports/verification/v0.1/20260829T145723+0800/summary.md) |
| V0.1 Agent 审计修复 | usage/request ID、提示词隔离、候选上限和业务库指纹已修复并完成真实双跑；[验证报告](reports/verification/v0.1/20260829T160420+0800/summary.md) |
| V0.2 Provider 契约 | 六家统一 Fixture CT 已实现；DeepSeek Nightly 与必需 Live 卡通过，[状态卡](reports/verification/v0.2/provider-status.json) 为 `live_verified=true`；其余 Provider 未 Live 验证 |
| 企业 Adapter | 未实现、未验证 |
| 远程 GitHub Actions | 当前未提交变更尚未在远程实跑 |

V0.1-T10 及后续 Agent 审计修复已完成真实 DeepSeek + 锁定 BGE 双跑；V0.2 Core、真实 DeepSeek Nightly 与必需 Provider Live 卡均已通过。Kimi、智谱、OpenAI、Anthropic 仍只有无网络 Fixture CT，不包含这些 Provider 的 Live 验证，也不包含企业 Adapter。Live 与 Enterprise 测试默认不运行；显式运行时必须同时提供开关和非空已知 target，否则以非零状态拒绝而不是冒充通过。

## 设计与安全

- [架构设计](Oria架构设计.md)
- [详细执行路线](docs/Oria详细执行路线.md)
- [V0.1 威胁模型](docs/security/V0.1威胁模型.md)
- [ADR 索引](docs/adr/README.md)
- [验证证据模板](reports/verification/TEMPLATE.md)

依赖必须通过 `uv.lock` 同步；不得在个人环境绕过锁文件安装。仓库不提交密钥、令牌、真实客户数据或 `.env`。
