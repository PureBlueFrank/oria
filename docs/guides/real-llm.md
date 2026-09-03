# 真实 DeepSeek 快速开始

Oria 已内置 DeepSeek Responses profile，默认模型为 `deepseek-v4-flash`。真实 LLM 只参与招商草案生成和硬资格候选集内的软排序；硬规则、权限、审批和业务写入仍由本地确定性组件处理。

## 前置条件

使用锁定的 uv 0.12.6 和 Python 3.11，安装开发依赖与 `standard` extra。`standard` 包含本地 BGE 所需的 sentence-transformers：

```bash
uv sync --locked --group dev --extra standard
```

## 用环境变量启用

四个变量必须同时配置。`runtime_profile=standard` 不允许 Fixture Embedder，因此不能遗漏 `ORIA_EMBEDDING_PROFILE=bge`：

```bash
export ORIA_RUNTIME_PROFILE=standard
export ORIA_LLM_PROFILE=deepseek
export ORIA_EMBEDDING_PROFILE=bge
export DEEPSEEK_API_KEY='你的 Key'
```

先做脱敏的配置检查，再运行真实 DeepSeek + 本地 BGE 的只读提案：

```bash
uv run oria config doctor --output json
uv run oria demo --output human
```

首次使用 BGE 需要下载锁定的模型 revision，所以会比后续运行更慢。

## YAML 等价写法

可在显式传给 `--config` 的 YAML，或 `~/.oria/config.yaml` 中选择相同 profile：

```yaml
runtime_profile: standard
llm:
  active_profile: deepseek
embedding:
  active_profile: bge
```

Key 仍通过环境变量注入：

```bash
export DEEPSEEK_API_KEY='你的 Key'
uv run oria demo --config /path/to/config.yaml --output human
```

不显式传 profile CLI 选项时，环境变量优先于 YAML，YAML 优先于内置默认值；显式的 `--runtime-profile`/`--llm-profile`/`--embedding-profile` 是最终的当次命令覆盖。

## `config doctor` 不等于 Live 验证

`oria config doctor` 只做以下静态检查：

- 解析默认值、YAML、环境变量和 CLI 覆盖。
- 展开当前 active profile 的变量并检查必填项。
- 验证 `edition` / runtime / LLM / embedding / storage 组合矩阵。
- 输出不含 secret 的配置投影和 fingerprint。

它不会请求 DeepSeek，也不证明 Key 有效、远端可达或模型调用成功。对个人体验而言，`oria demo` 成功完成才表明该次命令实际取得了远端响应；对仓库的正式“已 Live 验证”声明，还必须有保存 model、request ID、usage、配置和时间的脱敏 Live 验证卡。当前 DeepSeek 证据见 [V0.3-T09 报告](../../reports/verification/v0.3/20260903T004622+0800/summary.md)。

## 在完整 Workflow 中使用

Workflow 由多条独立 CLI 命令持续恢复。推荐在开始前导出上述四个环境变量，或使用同一 YAML，以保证 `workflow start`、`workflow resume`、`approval` 和 `mock` 每次重建 Runtime 时都使用相同 profile。完整操作步骤见 [本地 Workflow 手册](local-workflow.md)。

这些子命令也统一支持以下当次覆盖：

```bash
uv run oria workflow start \
  --runtime-profile standard \
  --llm-profile deepseek \
  --embedding-profile bge \
  --data-dir /tmp/oria-workflow \
  --thread-id scenario-a-live-001 \
  --campaign-id campaign-live-001 \
  --output human
```

但 profile CLI 选项需在每一条后续恢复/事件命令中重复，因此长流程更适合环境变量或 YAML。

## 成本、Key 和边界

- DeepSeek 会产生真实 API 费用；请在发起前检查账户限额与所在组织的成本政策。
- Key 只通过环境变量或 secrets manager 注入，不写入 YAML 样例、README、日志、报告或仓库。
- 真实 LLM 只作用于草案和候选集内软排序；商家/商品硬资格始终由确定性 Policy 执行。
- 券、招商、商品库、选品、C 端投放和 IM 仍使用 Mock Adapter 与合成数据，不代表任何真实企业系统已接入。
- 除 DeepSeek 外的 Provider 当前仅完成 Fixture 契约验证，不在“已 Live”列表中。
