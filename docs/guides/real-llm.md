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

上面的 `oria demo` 已经自动初始化默认的 `.oria-data`。完整 Workflow 会创建活动、券和投放等业务状态，因此请改用一个新的独立数据目录，避免与 demo 的 state 冲突。

因为你已 export 四个环境变量，以下每条独立命令都会自动使用 DeepSeek + BGE，无需重复传入 `--runtime-profile`、`--llm-profile` 或 `--embedding-profile`。先为整条流程准备固定标识；后续所有命令必须复用相同的 `DATA_DIR` 和 `THREAD_ID`：

```bash
DATA_DIR="/tmp/oria-workflow-live"
THREAD_ID="scenario-a-live-001"
CAMPAIGN_ID="campaign-live-001"
```

每个 Mock 事件的 `--source-event-id` 也要保持唯一。审批 ID 和业务确认任务 ID 不要预先填写，它们来自每次 human 输出的最新“下一步命令”。

### 1. 初始化并启动

`workflow start` 会自动初始化；这里先显式执行 `data init`，便于分开查看 migration 与播种结果：

```bash
uv run oria data init \
  --data-dir "$DATA_DIR" \
  --output human

uv run oria workflow start \
  --data-dir "$DATA_DIR" \
  --thread-id "$THREAD_ID" \
  --campaign-id "$CAMPAIGN_ID" \
  --request "生成华东餐饮招商活动并完成预定流程" \
  --output human
```

启动后的“当前阶段”应为“招商发布审批”，并显示含 `--approval-id` 的“下一步命令”。复制该参数的值：

```bash
LAUNCH_APPROVAL_ID="粘贴最新‘下一步命令’里的 --approval-id 值"
```

### 2. 批准 Launch 审批

批准 LaunchPlan，继续物化券批次并投放商家侧招商：

```bash
uv run oria approval approve \
  --data-dir "$DATA_DIR" \
  --thread-id "$THREAD_ID" \
  --approval-id "$LAUNCH_APPROVAL_ID" \
  --output human
```

批准后的“当前阶段”应为“报名与商品圈选”，“下一步命令”会提示关闭报名窗口。

### 3. 注入报名并关闭窗口

`mock enrollment` 只接收一条已认证的合成报名事件，不恢复 Graph；`mock window-close` 才解析等待并恢复 Workflow：

```bash
uv run oria mock enrollment \
  --data-dir "$DATA_DIR" \
  --thread-id "$THREAD_ID" \
  --source-event-id enrollment-event-001 \
  --merchant-id demo-m001 \
  --product-ref synthetic-product-demo-m001 \
  --output human

uv run oria mock window-close \
  --data-dir "$DATA_DIR" \
  --thread-id "$THREAD_ID" \
  --source-event-id window-close-event-001 \
  --output human
```

关窗后的“当前阶段”应为“动态业务确认”，“下一步命令”会给出当前 `--confirmation-task-id`。

### 4. 逐个处理动态确认链

`workflow resume` 每次只处理一个 ConfirmationTask。每一轮都从最新返回的“下一步命令”重新复制 `--confirmation-task-id`，不得重用已经决定过的 ID：

```bash
CONFIRMATION_TASK_ID="粘贴最新‘下一步命令’里的 --confirmation-task-id 值"
uv run oria workflow resume \
  --data-dir "$DATA_DIR" \
  --thread-id "$THREAD_ID" \
  --confirmation-task-id "$CONFIRMATION_TASK_ID" \
  --decision confirm \
  --output human
```

只要最新输出的“当前阶段”仍是“动态业务确认”，就从新的“下一步命令”复制 `--confirmation-task-id` 的值并重复上述命令。当前内置 Fixture 的冻结规则是 merchant → sales → sales_manager，共 3 级，因此本示例需要执行 3 轮；确认链由规则动态生成，不应把 3 级当成通用常量。

最后一轮确认成功后，“当前阶段”应变为“提交并等待招后选品”，“下一步命令”会提示注入选品完成事件。此时系统已自动完成券关联、提交选品并进入异步等待。

### 5. 注入选品决定与完成事件

先写入逐商品决定。该命令不恢复 Graph，返回的“当前阶段”仍是“提交并等待招后选品”：

```bash
uv run oria mock selection-decision \
  --data-dir "$DATA_DIR" \
  --thread-id "$THREAD_ID" \
  --source-event-id selection-decision-event-001 \
  --selection-version selection-v1 \
  --decision selected \
  --output human
```

再注入相同 `selection_version` 的完成事件，这次才会恢复 Graph：

```bash
uv run oria mock selection-complete \
  --data-dir "$DATA_DIR" \
  --thread-id "$THREAD_ID" \
  --source-event-id selection-complete-event-001 \
  --selection-version selection-v1 \
  --output human
```

返回的“当前阶段”应为“C 端发布审批”。复制最新“下一步命令”中 `--approval-id` 的值：

```bash
CONSUMER_APPROVAL_ID="粘贴最新‘下一步命令’里的 --approval-id 值"
```

### 6. 批准 C 端投放审批

```bash
uv run oria approval approve \
  --data-dir "$DATA_DIR" \
  --thread-id "$THREAD_ID" \
  --approval-id "$CONSUMER_APPROVAL_ID" \
  --output human
```

批准后，“当前阶段”为“通知商家并闭环”，终态文案应为“流程已完成: C 端投放与商家通知已闭环。”Business DB 中对应投放状态为 `published`，商家通知状态为 `sent`。

Mock 企业 Adapter 场景的更多说明、拒绝分支和重放排错详见 [完整本地 Workflow 操作手册](local-workflow.md)。

## 成本、Key 和边界

- DeepSeek 会产生真实 API 费用；请在发起前检查账户限额与所在组织的成本政策。
- Key 只通过环境变量或 secrets manager 注入，不写入 YAML 样例、README、日志、报告或仓库。
- 真实 LLM 只作用于草案和候选集内软排序；商家/商品硬资格始终由确定性 Policy 执行。
- 券、招商、商品库、选品、C 端投放和 IM 仍使用 Mock Adapter 与合成数据，不代表任何真实企业系统已接入。
- 除 DeepSeek 外的 Provider 当前仅完成 Fixture 契约验证，不在“已 Live”列表中。
