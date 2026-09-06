# V0.4-T05 DeepSeek Live 评测

## 结论

- 执行状态：**60/60 完整完成，自动质量验证失败，FrankLee 已确认盲评失败结论**。
- 原始 Live JSON 状态：`completed_pending_human_review`；该字段是运行结束时生成的不可变原始状态，不表示质量通过。后续 `human-review.md` 已记录 `FrankLee` 的失败结论确认。
- 自动通过：7/60（11.67%）；其余 53 次均未形成可被本地契约接受的最终结论。
- V0.4-T05 已以 **failed** 验证卡收口，不得声明场景 B 真实模型归因质量通过；进入后续实施前应先明确接受该风险或建立新的修复与重验版本。
- 被测数据均为版本化合成数据，不含真实企业数据。

## 冻结身份与运行完整性

- target / model：`deepseek` / `deepseek-v4-flash`
- dataset version / SHA-256：`1` / `b59078792fd00c3fff916183679402cf4cc340da98f7da27bee3dcea4923e40d`
- rubric SHA-256：`d91fbc296d3ff9eb175bdb4a3949f6c80d4b0a7b452f179bed263f8e2ed026af`
- Fixture baseline：`sha256:9daf97daf230cc40fc064bac7eaecb2d18f82b3cb790121175bf7ba2e99d87c4`
- Live eval fingerprint：`sha256:17db1bb9d61e7a7e7f78257a540f0489d12f55fb2249ab1fc740126e42e8c6ba`
- 时间：`2026-09-06T10:46:03.900555+08:00` 至 `2026-09-06T11:30:18.325783+08:00`
- 覆盖：20 个唯一 Holdout，各执行 3 次；每次重复均为 20 条。
- request ID：199 个，199 个唯一；全部记录的 Provider 模型均为 `deepseek-v4-flash`。

## 硬预算与用量

- 单例上限：4 次模型调用、10 次只读工具、96000 input token、24000 output token、0.075 美元和 240 秒。
- 本轮总上限：60 个 case run、240 次模型请求、576 万 input token、144 万 output token、1.35 美元和 7200 秒。
- 实际上界用量：199 次模型请求、1,497,278 input token、354,203 output token、1.12635028 美元。
- 费用口径：`pricing_upper_bound`，按定价快照并假设 input 全部 cache miss。
- 包含此前三次失败运行和四个开发探针后，累计上界费用约 1.69743596 美元，低于用户授权的 2 美元。

## 自动指标

| 指标 | 结果 |
| --- | ---: |
| 自动通过率 | 11.67% |
| outcome accuracy | 11.67% |
| abstain accuracy | 11.67% |
| 必需工具覆盖 | 94.17% |
| 禁止工具安全率 | 100% |
| grounded evidence rate | 0% |
| answer coverage | 0% |

7 次被契约接受的输出全部为正确的 `insufficient`。没有 `attributed` 或 `conflicting` 输出通过最终证据校验，因此 coverage-risk 只覆盖这 7 次低覆盖样本；其 Brier/ECE 仅具描述意义，不能作为质量通过依据。

三次重复的自动通过率分别为 10%、10%、15%，标准差 2.36 个百分点；每案例 outcome 一致率为 80%。平均单例时延分别为 47.37 秒、43.39 秒、41.36 秒。

## 失败分布与根因

| 终止原因 | 次数 |
| --- | ---: |
| `max_tool_calls` | 16 |
| `max_model_turns` | 14 |
| `policy_or_contract_violation` | 10 |
| `structured_output_error` | 10 |
| `evidence_validation_failed` | 2 |
| `schema_validation_failed` | 1 |
| 成功完成 | 7 |

其中 30 次在反复调查中触及工具或模型轮数上限；10 次工具批次拒绝由 9 次 `invalid_arguments` 和 1 次 `unknown_tool` 构成。结构化输出错误的安全消息以无效 JSON 为主。Provider 身份、request ID、租户权限和只读安全边界均未失守。

这表明当前主要问题不是 Golden 因果关系，而是公开模型在现有工具与严格结构化输出契约下无法及时停止调查、生成合法工具参数并提交可校验结论。后续修复应先在 development split 上完成，不得继续针对已运行的 Holdout 逐题调参；再次宣称无偏 Live 质量需要新冻结评测版本或明确将本轮视为已暴露的回归集。

## “报名和核销同时下降”案例

对应 Holdout `sb-v1-043`。冻结参考结论仍成立：访客趋势支持上游访问/曝光恶化是报名与核销数量下降的一个独立贡献，但不能解释同时存在的核销/确认下降；因此应保留“上游流量损失”和“下游核销转化恶化”两个并行贡献，不能压成单一先后因果。

证据为：混合漏斗变体中，华东正餐访问/曝光从 57.95% 降至 37.93%，核销/确认从 69.93% 降至 44.58%，报名/访问基本稳定在 48.15%→47.97%，同期曝光日均约 1788.46→1783.50。该链条先定位两个独立变化环节，再限制因果措辞；没有把“报名下降导致核销下降”或“核销下降导致报名下降”当成证据。

DeepSeek 对该案例的三次 Live 均未产出结论：两次终止于 `max_model_turns`，一次终止于 `max_tool_calls`。因此本轮只能确认冻结案例的证据设计正确，不能声称真实模型已通过该因果分析。

## 证据文件

- `live-run.json`：逐例结果、工具结果、事件、request ID、Token、成本、方差与 coverage-risk。
- `blind-review.json`：原始 10 条去标签盲评包。
- `human-review.md`：`FrankLee` 授权的 AI 辅助代评已完成，10/10 均失败，并由 `FrankLee` 于 `2026-09-06T14:13:14+08:00` 明确确认；该记录签署本轮失败结论，不冒充成功答案的独立人工语义校准。
- SHA-256：`live-run.json` 为 `15d791788c43869c1d89a0c4cc39e26ee21cf12f19d7f3d1aee5e4184362fe50`；`blind-review.json` 为 `08f123a9aaad5314d7c11bc49756ef9b6c52a06c67164e266e7dc97bfc495390`。

## 本地回归验证

- `UV_OFFLINE=1 UV_FROZEN=1 make lint`：通过；279 个文件格式检查、Ruff 和 143 个源文件的 mypy 均通过。
- `UV_OFFLINE=1 UV_FROZEN=1 make test`：791 passed、1 deselected、4 warnings，耗时 173.67 秒；4 条均为既有 SQLite migration 外键反射告警。
- `make smoke`：通过，`oria --version` 输出 `0.1.0`。
- 标准离线 `make build` 首次因临时 uv 缓存不含 `hatchling` 而失败；随后复用本机已缓存且锁定范围内的构建后端，以 `uv build --offline --no-build-isolation` 成功生成 sdist 和 wheel，未联网或安装依赖。
- `git diff --check`：通过。

## 前序失败证据

- `reports/verification/v0.4/20260906T000156+0800/`：首次真实运行，身份信任装配错误导致大量权限拒绝。
- `reports/verification/v0.4/20260906T001329+0800/`：身份修复后运行，原单例 Token 预留不足。
- `reports/verification/v0.4/20260906T103034+0800/`：兼容性与工具约束修复后运行，11/60 时再次被原 Token 预留中止。

这些失败尝试均独立保留，不与最终 60 条报告合并，也未被描述为通过。
