# V0.4-T05 Live 评测链路与零请求预检

## 结论

- 任务状态：**进行中 / blocked**。
- 实现状态：Live harness、冻结身份预检、硬预算、20×3 分层重复、逐例原始结果、方差、置信度分桶、coverage-risk 和至少 10 条人工盲评包契约已建立。
- Live 状态：未执行。当前进程缺少 `DEEPSEEK_API_KEY`，预检在 Provider 创建前阻断，`request_count=0`。
- 声明边界：本卡不证明 DeepSeek 在场景 B 的归因质量，不证明 S1–S3 Live 通过，也不能替代人工校准证据。

## 冻结身份

- target：`deepseek`
- model：`deepseek-v4-flash`
- dataset version / SHA-256：`1` / `b59078792fd00c3fff916183679402cf4cc340da98f7da27bee3dcea4923e40d`
- rubric SHA-256：`d91fbc296d3ff9eb175bdb4a3949f6c80d4b0a7b452f179bed263f8e2ed026af`
- Fixture baseline fingerprint：`sha256:9daf97daf230cc40fc064bac7eaecb2d18f82b3cb790121175bf7ba2e99d87c4`
- Holdout：20 条，3 次重复，预期 60 个 case run；覆盖 `attributed/conflicting/insufficient`。
- 定价快照：`deepseek-20260830`，在预检日期仍有效。

## 硬预算

- 总量：60 个 case run、480 次模型请求、1920000 input token、480000 output token、2 美元、7200 秒。
- 单例：8 次模型调用、6 次只读工具、32000 input token、8000 output token、0.05 美元、240 秒。
- 每例调用前先预留最坏情况 Token/成本额度；返回后按 Provider 用量结算。Provider 未返回成本时，按定价快照和全部 cache miss 记录上界估算，报告明示 `cost_basis`。

## 评测口径

- 自动指标：结果三态、abstain、必需工具覆盖、禁止工具安全、ToolResult 引用可回查与运行终止。自动指标不使用参考答案的原句匹配代替语义评分。
- 置信度：模型自报 confidence 对正确弃答转换为 `1-confidence`，得到 decision confidence；固定报告 0/0.5/0.7/0.8/0.9 阈值，不事后挑阈值美化结果。
- Brier/ECE：仅作描述。原因是 60 个 run 来自 20 个重复案例，样本相关，且语义正确性尚待人工校准。
- 盲评：至少 10 条，隐藏 split、critical、根因标签、参考假设/证据、Golden rationale、预期工具、Provider 和架构标签。本轮未运行 LLM judge，避免将同模型自评当成人工校准。

## 实际验证

1. T05 定向测试：`8 passed in 15.33s`。
2. 全量非 Live/Enterprise/Performance 回归：`789 passed, 1 deselected, 4 warnings in 179.58s`；4 条为既有 SQLite migration downgrade 告警。
3. 静态检查：Ruff format/Ruff check 通过，mypy 为 `Success: no issues found in 143 source files`。
4. 当前项目环境 CLI smoke：`oria --version` 输出 `0.1.0`。
5. 离线构建：复用本机已缓存的构建后端完成 sdist/wheel，产出 `oria-0.1.0.tar.gz` 和 `oria-0.1.0-py3-none-any.whl`；wheel 内已核对包含 `oria/eval/attribution_live.py`。未完成全新隔离环境的 wheel CLI smoke，因临时 uv 缓存中不含全部运行依赖，且当前网络被禁止。
6. 零请求预检：退出码 2，`status=blocked`，`request_count=0`，原因为 `attribution Live credential is missing`。
7. 预检已确认 dataset version/hash、20 条 Holdout、3 次重复、60 个预期 case run 和定价快照身份均一致。
8. 机读卡：[`preflight.json`](preflight.json)。

## 剩余验收项

1. 为当前进程提供非空 `DEEPSEEK_API_KEY`，重跑同一冻结配置。
2. 完整执行 60/60 case run，不接受部分样本冒充通过。
3. 人工独立评分至少 10 条盲评项，记录审阅人、时间、分项评分与分歧。
4. 根据实际结果出具 passed 或 failed Live 卡；在此之前 V0.4-T05 保持 blocked。
