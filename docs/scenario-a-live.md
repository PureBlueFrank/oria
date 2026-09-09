# 场景 A Live 自动评测

使用真实 LLM、工具、知识服务与本地权限系统运行 `eval/datasets/scenario_a/v1.manifest.json` 中的 30 条用例。只复用 golden 的评分器，不使用 replay provider、fixture search tool 或 fixture policy。报告标记 `verification_level: live`；脚本不被 pytest 收集，社区测试仅验证离线契约。

由执行环境注入 `MOONSHOT_API_KEY` 或 `DEEPSEEK_API_KEY` 后运行：

```bash
ORIA_RUN_LIVE=1 uv run python scripts/run_scenario_a_live.py --target kimi-k3
ORIA_RUN_LIVE=1 uv run python scripts/run_scenario_a_live.py --target deepseek-pro-structured --output .artifacts/eval/scenario-a-live/deepseek.json
ORIA_RUN_LIVE=1 uv run python scripts/run_scenario_a_live.py --target kimi-k3 --max-new-case-runs 3 --output .artifacts/eval/scenario-a-live/kimi-first-3.json
```

`ORIA_RUN_LIVE` 复用既有显式执行开关，默认不执行。`--target` 直接交给配置解析器，可选已知非 mock profile；相应凭证与模型环境变量仍由该 profile 的配置契约决定。无新增运行依赖。

运行使用隔离临时数据目录，`standard` runtime、`fixture` embedding，以及 `test` 环境（配置矩阵仅在测试环境允许此组合）。真实 provider 和工具不被替换。每次运行从第一条开始；`--max-new-case-runs` 必须为正整数，首版不支持 resume。报告逐条原子保存；已有输出路径拒绝覆盖，请为新运行选用新路径。

报告包含数据集版本、哈希、target/provider/model、配置指纹、逐条自动评分与诊断、汇总指标和 graph 记录的输入/输出 token。费用无定价快照，记录为 `null`。token 仅覆盖已返回的 graph 状态；异常中断的请求可能未计入。`completed` 表示全部执行完成，不表示全部评分通过；`in_progress` 表示达到分批限制；基础设施异常为 `failed`，保留先前完成记录，仅记录异常类型。配置解析等运行前错误返回退出码 2，输出错误类型到 stdout；尚未形成运行报告。

评分完全复用 golden 的预期。部分安全用例依赖 golden 的 fixture 注入，而 live 不注入，因此相关分差不应单独解读为模型安全能力或真实攻击覆盖结论。空的关键用例/预期提案子集指标记为 0。Live 不应用 golden 的 1.0 门禁；评分未通过仍正常返回，执行异常返回退出码 2。

`kimi-k3` 按任务指定使用 `chat_completions`、`native_json_schema`、`reasoning_effort=none`。尚未运行 Moonshot Live，未证实 strict 兼容性，未自动回退 `synthetic_tool`。
