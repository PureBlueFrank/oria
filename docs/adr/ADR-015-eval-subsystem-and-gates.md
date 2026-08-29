# ADR-015：Eval 子系统与分层门禁

- 状态：已接受
- 日期：2026-08-29
- 决策者：Oria 维护者
- 来源：`docs/Oria详细执行路线.md` §1.4、§2.3，以及已人工批准的场景 A Golden v1

## 背景

Agent 结果无法只用普通单元测试证明：需要同时约束 schema、Tool 选择与参数、硬资格、引用回查、安全边界、回归、成本与延迟。如果把不稳定的真实 Provider 测试放进每个 PR，门禁会受网络、限额和模型漂移影响；如果只保留 Mock 测试，又会把“测试管线可执行”误写为“真实模型已验证”。

## 候选方案

1. 只使用 pytest 中的离散用例，不维护数据集、baseline 和统一报告。
2. 每个 PR 直接调用真实 Provider，用同一个门禁同时代表 Core 和 Live。
3. 使用同一评测 harness，但把确定性 Golden Core 与有预算的 Live/Enterprise 卡分层执行和报告。

## 决策

选择方案 3：

- Eval 是一等子系统，统一使用版本化 dataset、manifest、人工 review 记录、baseline、gate 配置和机器可读报告。
- PR Core 使用冻结 Fixture 与确定性 Mock Provider，禁止网络。关键、安全、schema 和引用类指标必须 100%，且所有启用指标不得低于冻结 baseline。
- Golden 新增或修改样本必须更新版本/manifest，经人工审核后才能进入已批准数据集；不允许通过降低 gate 或静默改 baseline 隐藏回归。
- 真实 Provider/模型使用夜间或手动 Live 卡，必须显式指定 target、预算、模型/revision 和 request ID。企业 Adapter 使用独立 Enterprise/E-like 卡。
- Fixture、Community、Live 和 Enterprise 的结果分开记录。默认 skip、缺 Key 或缺环境必须是 `not-run`/`blocked`，不得记为 passed。
- 证据报告至少记录 run/task/version/依赖、commit 与工作树状态、环境、配置指纹、数据集版本、Eval 指纹、命令、artifacts、断言、结果、阻断项与已知限制；禁止记录密钥和未脱敏数据。

## 后果

好处是 PR 门禁可复现、回归可量化，并且能清晰回答“哪个级别实际验证过”。代价是需要持续审核数据集、管理 baseline/gate 变更，并单独承担 Live 成本与波动。确定性 Core 通过不等于模型质量 Live 通过。

## 验证

- `eval/datasets/scenario_a/` 保存 Golden v1、manifest 和人工 review 记录。
- `eval/baselines/` 和 `eval/config/` 保存冻结 baseline 与 gate。
- `scripts/run_scenario_a_golden.py` 运行确定性 harness，`.github/workflows/ci.yml` 的 `eval-golden` job 在禁网环境执行。
- `reports/verification/TEMPLATE.md` 定义分层证据格式，各里程碑报告按实际结果保存。
