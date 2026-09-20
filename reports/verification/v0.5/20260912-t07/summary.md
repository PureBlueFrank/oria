# V0.5-T07 Live 对照准备（2026-09-12）

- 状态：用户已批准订阅通道，按 80 次 / 116 美元合计等价预算方案执行中；不宣称 T07 完成或多智能体质量提升。
- 基线：`60e0322b69771c41815c5aa03c89c5e46d113d53`，开始时工作区干净；本报告对应其上的 T07 局部修改。
- 预检：[preflight.json](preflight.json)，`ready`、真实模型请求数 0。修复后复检 [preflight-final.json](preflight-final.json) 仍为 `ready`、请求数 0。订阅登录状态可用；未读取或保存凭证。
- 目标：`codex-subscription-gpt56-sol-high`，既有配置锁定 GPT-5.6 Sol / high，使用 ChatGPT 订阅通道。数据为已审阅冻结的 Scenario B V2 holdout，不更改数据或 rubric。

## 已批准的执行范围

20 条 holdout × 2 次重复 × single/multi，共 80 次架构运行。按 `comparison-live-v1` 固定随机顺序执行，保留所有结果，不筛选最佳重复。

| 预算 | 每种架构 | 两种架构合计 |
| --- | --- | --- |
| 架构运行数 | 40 | 80 |
| 模型请求上限 | 320 | 640 |
| 输入 Token | 8,000,000 | 16,000,000 |
| 输出 Token | 1,280,000 | 2,560,000 |
| API 等价成本上限 | 58 美元 | 116 美元 |

成本使用仓库冻结的 `openai-20260909` 快照核算，仅作对照计量，并非本次订阅调用的实际 API 账单或实时价格报价。每次运行最多 8 模型轮、10 工具调用、200,000 输入 / 32,000 输出 Token、1.45 美元等价成本、600 秒。真实企业接口不在本次范围内。

批准后每次只新增 1 条运行并落盘，随后以 `--resume` 继续，直到完成全部 80 条。遇到额度、传输或未知失败停止；核实失败请求与消耗后才能决定续跑，不自动重试以挑选结果。当前 runner 在一次调用末尾保存结果，不提供调用中崩溃的逐请求恢复保证，因此不能将未记录的失败请求当作零消耗。

首条运行命令（仅在批准后执行）：

```sh
uv run python scripts/run_comparison_live.py \
  --run-live --target codex-subscription-gpt56-sol-high \
  --max-new-case-runs 1 \
  --data-dir .artifacts/eval/comparison-live-t07-20260912 \
  --output .artifacts/eval/comparison-live-t07-20260912/run.json
```

后续使用同一命令追加 `--resume`。失败保存为独立 `run.failure-*.json`，不覆盖既有结果。预检使用单独路径，不覆盖 run 文件。

## 本地修复与证据边界

1. 原 Live 路径默认使用 `fixture_judge`，仅检查回答结构，却产生质量提升/退化结论。现在显式标记 `quality_basis=structural_proxy`；默认 Live 完成后结论为 `pending_human_review`。结构分及其差值不能支持真实质量声明。
2. 每条运行保存架构隐藏的 `judge_packet`；脚本另导出 `run.blind-review.json`，供独立审阅。审阅者只接收该文件与冻结 rubric，不接收含架构映射的 run 报告。人工评分和审阅身份另存，完成前不对外宣布质量结论。
3. 脚本要求 `--run-live`，拒绝意外覆盖已有输出；原子替换报告，失败另存。恢复前核对数据 hash、rubric、模型、重复次数、seed、计价标识与两侧预算。

## 验证与失败历史

- 修改前对照契约与集成基线：12 passed。
- 先增加回归断言：2 failed，复现缺少结构评分标识与盲评回答保存；失败保留于此，不作为 Live 失败。
- 报告保护回归首先 1 failed，复现缺少失败旁路保存；修复后相关检查通过。
- 第一轮修复后定向检查：13 passed；随后新增显式运行开关与覆盖保护检查，最终脚本定向回归 3 passed，完整套件另行记录。
- 首轮静态检查仅格式失败，已格式化受影响文件；最终 `make lint` 通过（163 个源文件类型检查）。
- 完整非 Live/Enterprise/Performance 回归：`uv run pytest -m 'not live and not enterprise and not performance' -q`，981 passed、1 deselected、4 warnings，335.84 秒，退出码 0。告警来自 SQLite 外键反射，不在本次修改范围；不以告警代表测试失败。
- 上述本地准备检查未运行 Live、Enterprise、Performance；批准后的 Live 进展见下方。未推送，未核查远端 CI。

## 未完成项

目标与预算已批准；额度恢复后继续剩余 51 条真实对照，收集脱敏调用证据、完成独立盲评，汇总质量、成本、延迟和重复方差并报告 passed/failed/blocked。历史场景 B Live 仅作引用，不替代此次 single/multi 结果。

## Live 执行记录（首轮）

- [授权与额度记录](authorization.json)：执行前五小时额度已用 56%，周额度已用 9%；达到 90% 前暂停，不擅自使用额度重置。
- [沙箱启动失败](startup-failure-sandbox.json)：`ProviderUnavailable`，未形成完成案例；获准沙箱外执行后继续，原记录保留。
- 首两条运行已保存，均为 multi、零模型轮次，分别以 `unsupported_request` 与 `subagent_failed:subagent_tools_unavailable` 终止。保留这两条，不能改写或排除路由失败。
- 原始结果位于 `.artifacts/eval/comparison-live-t07-20260912/run.json`，盲评文件与失败旁路保存在同目录。执行结束或暂停时导出脱敏快照并更新此卡。

### 首轮暂停状态（保留历史）

已保存 **4/80 条**：[脱敏运行快照](run-paused-4.json)、[盲评材料快照](blind-review-paused-4.json)、[暂停与汇总](pause-summary.json)。两条为实际模型运行，两条为零模型调用的路由终止；模型轮次合计 4、工具调用 4、输入 87,738 Token、输出 3,685 Token、API 等价成本 **0.424652 美元**。结构评分不作为质量验收。

五小时账户额度已用 81%（账户共享用量，不能全部归因本实验），距离 90% 停批线的余量不足以稳妥启动最多 8 轮的新案例，因此主动暂停；预计重置时间为 **2026-09-12 22:10:14 +08:00**，周额度已用 13%。从冻结位置 4（第 5 条）使用原命令加 `--resume` 继续，原授权有效，无需重跑前四条。

证据限制：当前 comparison runner 未导出 Provider request ID，因此这里仅记录 graph model turns，不宣称已完成 request ID 唯一性核验。恢复前应补足脱敏调用追踪，并保留此前缺失事实；不得伪造 ID。本轮未修改模型、数据、rubric、调度顺序或 agent 行为。T07 仍为 **进行中 / 因订阅余量暂停**，没有单多架构效果结论。

## 2026-09-13 续跑：28/80 条后暂停

本轮从第 5 条续跑至第 28 条，新增 24 条，未重跑或排除任何案例。开始时五小时额度已用 0%；结束检查已用 **81%**、周额度已用 **26%**。保留足够单案例余量，在预计 **2026-09-13 03:52:04 +08:00** 重置前暂停。下一条为冻结位置 28（第 29 条），仍使用同一 target、预算、rubric、数据及 `--resume` 命令；原授权有效。

- [运行快照（28 条）](run-paused-28.json)、[盲评快照（28 条）](blind-review-paused-28.json)、[暂停汇总](pause-summary-28.json)。首轮 4 条快照不变。
- 累计 single 13 条、multi 15 条。18 条完成模型运行，10 条零模型调用路由终止（6 条 unsupported request、4 条 subagent tools unavailable）。这些是架构实际边界，保留为原始失败，不改变冻结代码去重跑。
- 累计 51 模型轮次、59 工具调用，输入 1,248,887 Token、输出 41,975 Token，API 等价成本 **5.835048 美元**。这不是订阅实际账单。
- 第 14 条保存后额度查询返回非 JSON 文本，控制脚本停止；只读复查成功后继续第 15 条。无模型请求重试，无已保存结果损失。
- 本轮未改动代码、模型配置、数据或评分标准，仅运行已有脚本并保存证据；未重复本地测试，沿用本卡 981 项测试及静态检查证据。文档差异检查通过；未推送，未运行 CI。
- request ID 导出缺口仍保留，不伪造或把模型轮次当作唯一请求数。独立盲评与剩余 52 条未完成，不能宣称多智能体质量提升。

## 后续续跑：29/80 条后暂停（2026-09-13T23:50:27.397941+08:00）

本轮只新增第 29 条，single 完成 2 轮模型调用，未错误终止，新增等价成本 0.175808 美元。累计 29/80 条，其中 19 条实际模型运行、10 条零调用路由终止；累计等价成本 **6.010856 美元**。前 28 条及冻结配置、数据、rubric、调度逐项保持一致。

开始五小时额度已用 71%，完成后账户共享用量已用 81%，周额度已用 71%；按既定余量规则暂停，预计重置 **2026-09-14T02:50:56+08:00**，不将共享额度增量全部归因本次评测。下一条为第 30 条，原授权有效。

证据：[运行快照](run-paused-29.json)、[盲评材料](blind-review-paused-29.json)、[暂停汇总](pause-summary-29.json)。历史快照不变。本轮未修改代码，复用已通过测试；已验证前缀结果与冻结资产一致，文档差异检查通过。剩余 51 条、独立盲评及 request ID 证据缺口仍未完成；没有质量提升结论。

## 2026-09-20 付费 Live 恢复前安全加固

本轮仅修改本地 harness/runner、契约测试和文档，**未执行 Live、Enterprise 或 Performance，未调用任何模型**。上述 29 条原始结果、历史快照和失败记录保持不变；本轮没有重跑、删除或筛选其中任何一条。T07 仍为进行中，不宣称质量提升。

安全加固后的恢复契约：

1. runner 以输出路径级 POSIX lock 强制单写者。resume 的 `runs` 必须是冻结 `execution_order` 的严格完整前缀；洞、顺序错、重复、非法槽位均在模型调用前拒绝。完成报告的 resume 同样先校验冻结契约、前缀、packet 和 sidecar，不再直接返回。
2. 受限状态 `.<output-stem>.state.json` 以 `0600` 保存。槽位开始前先原子写入 `in_flight` reservation；正常完成后在一次原子替换中记录 `pending_results` 并清除 reservation。崩溃或异常留下的 `in_flight` 以 `request_count=unknown` 阻断恢复；不伪造 request ID，不自动重跑该付费槽位。
3. 状态中的每 run 随机 256-bit secret 通过 HMAC 派生 opaque blind ID，并用独立 HMAC 顺序确定性洗牌导出包。secret 不进入主报告、盲评文件或公开验证快照。加固前的 `blind-review-paused-*.json` 仅作不可改写历史，不再作为正式独立盲评输入；首次安全 resume 会在不调用模型的情况下为已有 29 条重生成真盲化 packet。含架构映射的主报告保持在受限 `.artifacts` 运行目录，独立审阅者只接收 sidecar。
4. 每条 Live run 必须带有与 blind ID 一致的 `judge_packet`；缺失即拒绝，不静默过滤。主报告是权威结果，并保存 `blind_review_sha256`；主报告与 sidecar 先全部写临时文件、验证后再发布。sidecar 写入失败不发布新主报告，已完成报告的 resume 可由权威报告确定性重建并验 hash。
5. 冻结 binding 包含完整 selected target/hash、完整 pricing snapshot/hash、`rate_tier`、`tool_profile`、`termination_rule`、执行顺序 hash、等额预算与显式 `judge_basis`。同 ID 任何内容变更都拒绝 resume。旧 29 条的一次性补齐会先校验当前冻结配置、dataset/rubric、完整顺序、预算与逐条成本重算，不改变已保存观测。`structural_proxy` 通过显式枚举传递，即使 judge callable 被包装也仍只能得到 `pending_human_review`。

回归测试按要求先失败：新用例首次收集因缺少 secret 派生接口而中止，证明旧实现不满足契约。实现后相关 contract/integration 独立复验 `20 passed`；对 `.artifacts/eval/comparison-live-t07-20260912/run.json` 执行纯本地、只读兼容校验，确认 `29` 条是冻结顺序完整前缀、可补齐 binding，下一槽位仍为 **`position 29 / multi / sb-v2-034 / repetition 2`**。该校验没有调用 runner，没有改写原报告。

最终本地验证：

- 相关 contract/integration 独立复验：`20 passed in 48.41s`。
- `make lint`：通过；Ruff format 检查 331 个文件，Ruff lint 通过，mypy 检查 163 个源文件无问题。
- 完整非 Live/Enterprise/Performance：`uv run pytest -m 'not live and not enterprise and not performance' -q`，`991 passed, 1 deselected, 4 warnings in 620.89s`，退出码 0。告警为既有 SQLite 外键反射 `SAWarning`，本轮未改动相关 migration。
- `git diff --check`：通过。

本轮未运行 Live、Enterprise 或 Performance，未 commit/push，未运行远端 CI。安全加固已达到本地恢复前门禁；实际付费恢复仍需显式执行决定，并且必须从 **`position 29 / multi / sb-v2-034 / repetition 2`** 开始。
