# 场景 B 经营异常动态归因演示

`oria attribution ask` 是场景 B 的交互演示入口。它复用现有 attribution Graph、Runtime、五个只读分析工具和 replay provider，展示有界 `model → tools → validate` 调查循环：逐步查询、产生假设、用新证据交叉校验，最后输出归因、冲突、弃答或有界预算终止。

## 默认离线回放

```bash
uv run oria attribution ask
```

默认选择已审阅的 development 案例 `sb-v1-001`。命令强制使用干净的 Mock + Fixture 配置，不读取当前 shell 的 Provider 环境变量或个人 Oria 配置，不发起网络请求。Mock replay 会走过真实 Graph 与只读工具，适合检查运行时、证据回查和展示链路；回放路径来自已审阅案例，不能证明模型具有动态选路质量。

选择其他 development 案例：

```bash
uv run oria attribution ask --case-id sb-v1-015
```

也可以直接传问题，但仅做 Unicode NFKC、大小写和空白规范化后的全句精确匹配：

```bash
uv run oria attribution ask '为什么 2026-08-31 华东正餐招商核销转化率明显下降?'
```

输出会同时声明 `user_question`、实际送入 Graph 的 `question_asked` 和匹配方式。不做模糊匹配、语义猜测或自动选取“最像”案例；未知问题、未知 case、同时传问题和 `--case-id`、以及任何 holdout 选择均以退出码 2 拒绝，且不创建运行产物。

Human 输出是主视图，每一步都说明“做了什么、为什么做、依据什么、下一步为什么继续”。工具 ID 与参数只在末尾作补充。需要机器可读输出时使用：

```bash
uv run oria attribution ask --output json
```

## Live 真实模型

传入非 Mock 的 `--llm-profile` 后，命令会按现有配置优先级合并个人配置、环境变量和显式 profile，并将该 Provider 接入同一个 attribution Graph：

```bash
export DEEPSEEK_API_KEY='<your-key>'
uv run oria attribution ask --llm-profile deepseek-pro-structured
```

Live 模式用于观察真实模型是否会根据中间 ToolResult 改变下一步查询，并产生可回查的归因、冲突或弃答。这一命令会发生真实网络请求，并受 Graph 的模型轮次、工具调用、Token、成本、无进展和结构校验上限约束。单次演示成功不等于冻结 holdout 质量门禁通过；历史 DeepSeek 冻结 Live 卡仍为 failed，后续 GPT-5.6 Sol 已在冻结 V2 holdout 上通过严格人工盲评并成为场景 B 推荐 Live target。正式冻结评测及采用边界见 [attribution eval 入口](attribution-eval.md)。

## 运行产物与边界

每次命令都创建唯一目录，不覆盖之前的 fixture：

```text
<data-dir>/reports-tmp/attribution/<case-id>/<unique-run>/
├── attribution.json
├── fixture/<variant>/analytics.db
├── fixture/<variant>/evaluation-only/labels.db
└── runtime/...
```

默认 `<data-dir>` 是 `.oria-data`。该命令的 fixture、Runtime 和报告只写入 `reports-tmp`；`run_id` 始终精确绑定 `case_id`，重复运行通过外层唯一目录隔离。分析工具只能读取无标签的 `analytics.db`；标签库仅由现有合成 fixture 生成器物理隔离，不会注入 Graph、Prompt 或工具。

冻结基线、全量 split、指标与盲评包仍使用[attribution eval 入口](attribution-eval.md)，不用 `attribution ask` 替代。
