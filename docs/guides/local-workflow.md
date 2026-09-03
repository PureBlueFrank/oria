# 完整本地 Workflow 操作手册

本手册用本地 SQLite、官方 AsyncSqliteSaver、合成数据和 Mock 企业 Adapter 跑完招商场景 A。它会创建本地业务记录并执行 Mock 副作用，不会访问真实券、招商、商品库、选品、C 端或 IM 系统。

## 1. 准备共享标识

先同步锁定依赖：

```bash
uv sync --locked --group dev
```

为本次流程选择独立数据目录、thread 和 campaign ID。后续所有命令必须复用相同的 `DATA_DIR` 和 `THREAD_ID`：

```bash
DATA_DIR="/tmp/oria-workflow-local"
THREAD_ID="scenario-a-local-001"
CAMPAIGN_ID="campaign-local-001"
```

| 标识 | 来源 | 用途 |
| --- | --- | --- |
| `DATA_DIR` | 运行者选择 | 持久化 Platform DB、Business DB、Checkpoint 和本地投影 |
| `THREAD_ID` | 运行者选择 | 定位同一条 LangGraph 恢复游标 |
| `CAMPAIGN_ID` | 运行者选择 | 定位本次合成招商活动 |
| `approval_id` | 最新输出的 `interrupts[0].approval_id` | 决定 Launch 或 C 端投放审批 |
| `confirmation_task_id` | 最新输出的 `interrupts[0].confirmation_task_id` | 每次只恢复一个业务确认任务 |
| `source_event_id` | 运行者为每个 Mock 事件选择的唯一 ID | inbox 幂等与重放防护 |
| `selection_version` | 运行者选择，后续事件必须一致 | 绑定选品决定、完成事件和 C 端投放 |

## 2. 初始化并启动

`workflow start` 会自动初始化；下面先显式执行 `data init`，便于分开查看 migration 与播种结果：

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

启动结果应为 `status: interrupted`，且 `interrupts[0].kind` 为 `launch_approval`。复制该对象的 `approval_id`：

```bash
LAUNCH_APPROVAL_ID="粘贴 interrupts[0].approval_id"
```

## 3. 决定 Launch 审批

批准 LaunchPlan：

```bash
uv run oria approval approve \
  --data-dir "$DATA_DIR" \
  --thread-id "$THREAD_ID" \
  --approval-id "$LAUNCH_APPROVAL_ID" \
  --output human
```

或者拒绝并结束该分支：

```bash
uv run oria approval reject \
  --data-dir "$DATA_DIR" \
  --thread-id "$THREAD_ID" \
  --approval-id "$LAUNCH_APPROVAL_ID" \
  --reason "招商方案需要调整" \
  --output human
```

继续完整流程时选择 approve。批准后返回 `interrupts[0].kind: enrollment_window`。

## 4. 注入报名并关闭窗口

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

关窗后应进入 `interrupts[0].kind: business_confirmation`。

## 5. 逐个处理动态确认链

`workflow resume` 每次只处理一个 ConfirmationTask。每次都从最新返回重新复制 ID，不得重用上一步的已决定 ID：

```bash
CONFIRMATION_TASK_ID="粘贴最新 interrupts[0].confirmation_task_id"
uv run oria workflow resume \
  --data-dir "$DATA_DIR" \
  --thread-id "$THREAD_ID" \
  --confirmation-task-id "$CONFIRMATION_TASK_ID" \
  --decision confirm \
  --output human
```

只要最新返回仍是 `kind: business_confirmation`，就用新的 `confirmation_task_id` 重复上述命令。当前内置 Fixture 的冻结规则是 merchant → sales → sales_manager，共 3 级，因此需要执行 3 轮。确认链由规则动态生成，不应把 3 级当成通用常量。

要测试拒绝分支，将任意一轮的 decision 改为 `reject`：

```bash
uv run oria workflow resume \
  --data-dir "$DATA_DIR" \
  --thread-id "$THREAD_ID" \
  --confirmation-task-id "$CONFIRMATION_TASK_ID" \
  --decision reject \
  --output human
```

第三轮确认成功后，返回应变为 `kind: selection_event`，并含有用于观测的 `wait_id`。此时系统已自动完成券关联、提交选品并进入异步等待。

## 6. 注入选品决定与完成事件

先写入逐商品决定。该命令不恢复 Graph，返回仍是 `selection_event`：

```bash
uv run oria mock selection-decision \
  --data-dir "$DATA_DIR" \
  --thread-id "$THREAD_ID" \
  --source-event-id selection-decision-event-001 \
  --selection-version selection-v1 \
  --decision selected \
  --output human
```

`--decision` 可为 `selected` 或 `rejected`；拒绝时可增加 `--reason-code <code>`。再注入同一 `selection_version` 的完成事件，这次才会恢复 Graph：

```bash
uv run oria mock selection-complete \
  --data-dir "$DATA_DIR" \
  --thread-id "$THREAD_ID" \
  --source-event-id selection-complete-event-001 \
  --selection-version selection-v1 \
  --output human
```

返回应包含第二道审批 `interrupts[0].kind: consumer_publish_approval`。复制它的 `approval_id`：

```bash
CONSUMER_APPROVAL_ID="粘贴 interrupts[0].approval_id"
```

## 7. 决定 C 端投放审批

批准：

```bash
uv run oria approval approve \
  --data-dir "$DATA_DIR" \
  --thread-id "$THREAD_ID" \
  --approval-id "$CONSUMER_APPROVAL_ID" \
  --output human
```

或者拒绝：

```bash
uv run oria approval reject \
  --data-dir "$DATA_DIR" \
  --thread-id "$THREAD_ID" \
  --approval-id "$CONSUMER_APPROVAL_ID" \
  --reason "C 端投放参数需要调整" \
  --output human
```

批准后的成功终态是 `status: completed` 且 `interrupts: []`。Business DB 中对应投放状态为 `published`，商家通知状态为 `sent`。

## 8. 配置与 profile 一致性

`workflow start`、`workflow resume`、`approval approve/reject` 和全部 `mock` 子命令都支持：

```text
--config
--data-dir
--runtime-profile
--llm-profile
--embedding-profile
```

这些命令是独立进程，每次都会重建 Runtime。长流程推荐使用环境变量或同一 YAML，避免后续命令遗漏 profile 参数。真实 DeepSeek 所需的四个环境变量和安全边界见 [真实 DeepSeek 快速开始](real-llm.md)。

未显式传 profile CLI 选项时，环境变量优先于 YAML，YAML 优先于内置默认值；显式 CLI 选项保持现有的当次命令覆盖语义。

## 9. 重放与排错

- 不要更换 `DATA_DIR` 或 `THREAD_ID`，否则命令无法找到原 checkpoint 和业务状态。
- 每个 Mock 事件使用稳定且唯一的 `source_event_id`；同 ID 重放由 inbox 去重，同 ID 不同内容会被拒绝。
- 始终从最新 `interrupts[0]` 取得当前 ID。已解析、过期、跨 tenant 或与参数/checkpoint/策略版本不匹配的审批会 fail closed。
- `config doctor` 只校验配置，不读取 Workflow 状态，也不证明外部系统连通。
- 需要查看表结构与真相源时，参阅 [数据模型与核心表](../reference/data-model.md)。
