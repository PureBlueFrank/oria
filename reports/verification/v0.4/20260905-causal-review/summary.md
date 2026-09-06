# V0.4-T04：50 条案例的证据与因果修订

- 日期：2026-09-05（Asia/Shanghai）。
- 本次范围：依据原始合成事实修订全部 50 条参考案例、生成器、可读审阅材料及必要回归检查。
- 整体里程碑：V0.4-T04 已完成；`FrankLee` 已审阅全部 50 条案例，Holdout 与 Fixture baseline 已冻结。
- 本次修订验证：通过；下一未完成项为 V0.4-T05 真实模型质量验证。
- 分组：原案例 ID 与 development 30 / holdout 20 不变。
- 数据：`scenario_b_synthetic_v3`，固定 seed `20260902`；新增受控证据变体，生产分析 Tool 和权限边界未改变。

## 修订原则与结果

先核查现象和口径，再定位变化环节，再考虑业务原因。时间相邻、活动覆盖、两个猜测和没有查到记录，分别不能自动等同于确定因果、活动效果、证据冲突和现实中不存在。漏斗采用核销/确认，大盘采用核销/报名；汇总比例按数量加权。

当前为 20 条可回答（`attributed`）、24 条证据/权限不足（`insufficient`）和 6 条真实冲突证据（`conflicting`）。8 条非空 `root_cause_code` 只用于受控合成变体内的有限归因；它们由处理区域的介入前后反转、稳定区域/大盘对照和相邻漏斗环节共同限定，不外推为真实业务机制。

第 20 条“报名和核销同时下降”已改为上游访问/曝光和下游核销/确认同时恶化的受控变体；结论保留两个并行贡献，不虚构两者的先后因果。第 19/21/22/24/43 条分别覆盖大盘同向、重叠事件、品类共同变化、饮料多信号和多轮并行异常。第 39–44 条向 Graph 实际注入完整 user/assistant 历史消息对。

可读全文：`eval/datasets/scenario_b/CASES.md`。数值、时间窗口及活动记录由实际只读 SQLite 查询独立复算，不依赖 Mock 回放答案。

## 验证记录

- 批准前定向验证：归因 Agent、Golden 事实、合成变体、Eval 和盲评 rubric 共 **102 passed**；批准后状态、baseline 与 CLI 定向回归 **97 passed**。
- 回归覆盖 50 条逐例事实映射、11 类受控证据变体、原因弃答边界、冲突信号存在性、多轮历史顺序、生成器/JSONL/可读全文一致性及人工审阅门禁。
- `UV_OFFLINE=1 UV_FROZEN=1 make lint`：**通过**，276 个文件格式检查、Ruff 检查通过，mypy 检查 142 个源文件通过。
- `UV_OFFLINE=1 UV_FROZEN=1 make test`：**784 passed, 1 deselected, 4 warnings**，最终跑耗时 175.30 秒；4 条为已有迁移测试的 SQLAlchemy 外键反射警告。
- `UV_OFFLINE=1 UV_FROZEN=1 make build`：通过，生成 wheel 与 sdist；首次未带离线开关的尝试因网络策略拒绝 PyPI 解析，随后按锁文件离线重跑成功。
- `UV_OFFLINE=1 UV_FROZEN=1 make smoke`：通过，CLI 返回 `0.1.0`。
- 批准记录：审阅人 `FrankLee`，完成时间 `2026-09-05T18:46:54+08:00`；每条 case 的 review 字段均已写入。
- 正式 Fixture baseline：50/50 通过，全部结构化指标为 `1.0`，`eval_fingerprint=sha256:9daf97daf230cc40fc064bac7eaecb2d18f82b3cb790121175bf7ba2e99d87c4`。
- `git diff --check`：通过。
- 所有验证使用 uv **0.12.6**、Python **3.11.15**、项目锁文件及仓库内 `UV_CACHE_DIR=.artifacts/uv-cache`，未安装或升级依赖。
- 冻结 JSONL SHA-256：`b59078792fd00c3fff916183679402cf4cc340da98f7da27bee3dcea4923e40d`，与 manifest 及 baseline 一致；rubric SHA-256 为 `d91fbc296d3ff9eb175bdb4a3949f6c80d4b0a7b452f179bed263f8e2ed026af`。

## 验证边界

- Fixture：Mock 回放仅验证流程和冻结参考的确定性；本次人工批准由 `FrankLee` 提供，但仍不等于真实模型因果推理正确率。
- Community：使用真实本地 SQLite 查询复算合成事实；不代表真实业务数据。
- Live / Enterprise / Performance：均未执行。
- 回放查询参数已根据案例区域、品类、商家和对照需求动态绑定，但回放 Provider 仍按参考答案输出；不得引用其通过率证明真实模型的业务因果质量。
- 此次未更改既有 T01 独立 seed 标签库；其历史根因标签不是这 50 条修订案例的证据。
