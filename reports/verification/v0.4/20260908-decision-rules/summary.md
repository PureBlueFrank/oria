# Scenario B 决策规则工程化：本轮本地验证

- 日期：2026-09-08
- 起点：main / `5e3f1d64c47d23c60fcb7ab48b8b2326cd483f23`，起始工作区干净。
- 范围：V0.4-T03/T05 的 validator、prompt v3、repair、no-progress 收尾及对应测试；仅新增本轮报告。
- 状态：本地门禁通过；未运行新 Live。

## 改动与约束

- 当前提交必须有决策审计，历史归档仍可读取；运行时强制执行新 Schema，不能依赖 Provider 自行验证。
- 请求范围不可用时 insufficient；范围内多支持候选时 conflicting；其余证据缺失或无候选时 insufficient；恰好一个支持候选且无缺口时允许 attributed。各 outcome 的结论、假设、弃答与请求数据字段必须一致。
- 候选排除要求独立反证引用；索引、非 null 原值、支持关系及真实 ToolResult 来源均检查。
- 修复保留已声明的任务类型、范围、数据缺口和此前有证据的候选；禁止通过删候选或改变问题骗过校验。草稿以 assistant 数据提供给修复轮，不提升为系统指令。
- Scenario B 重复调查无进展后关闭业务工具，进入一次最终提交；必要时有限修复，仍不合法则失败。沿用全部预算限制，Scenario A 原 no-progress 终止行为保持。
- Fixture replay 仅适配新增审计字段，不改变冻结数据、rubric 或 baseline；Live 指纹包含 prompt/Schema/决策契约版本。

## 验证记录

所有命令使用锁定项目环境，通过 `UV_CACHE_DIR=/private/tmp/oria-uv-cache` 将缓存放到允许写入的位置；没有安装额外依赖或运行 make sync。

| 阶段 | 实际结果 | 证据 |
| --- | --- | --- |
| 初始回归（实现前） | 新决策契约 6 项失败：现有模型不接受审计字段且不支持强制审计入口 | 本任务工具输出；未把历史数据改写成测试答案 |
| 首轮现有集成与回放 | 21 passed | `targeted-r1.log` |
| 扩展回归 r1 | 25 passed / 2 failed；新测试替身缺少必填 usage，触发 provider_failure | `regression-r1.log` |
| 扩展回归 r2 | 30 passed / 1 failed；新对抗替身试图原位修改不可变映射 | `regression-r2.log` |
| 替身修正后集成 | 17 passed | `regression-r3.log` |
| 首轮全量 | 862 passed / 4 failed / 1 deselected；上述新增测试替身问题，已保留失败历史 | `full-r1.log` |
| 最终全量 | **867 passed / 1 deselected / 4 warnings**，186.40 秒 | `full-r2.log` |
| 最终补充规则测试 | **15 passed**，含全量收集后新增的 1 项合法反证正向测试 | `decision-unit-final.log` |
| 静态门禁 | make lint 通过（格式、Ruff、mypy） | `lint.log` |
| 受保护资产 | 190 个 Git 跟踪的 eval/历史报告文件与基线 blob 一致 | `protected-assets.json` |

全量的 4 条 warning 均来自 SQLite migration 外键反射（SQLAlchemy SAWarning），未隐藏或豁免。最终代码与测试格式/Ruff 检查、Git diff whitespace 检查通过。

测试使用独立合成观察和 Mock Provider，覆盖同环节多候选、候选丢弃、缺失区分证据不能伪装 insufficient、维度不可用却声称成功、缺失审计、空比较、引用越界、有反证的合法排除、错误反复修复耗尽、无进展后事实报告/弃答、收尾再次请求工具被拒、预算不扩张、伪造引用立即失败及场景 A 回归。

## 验证边界

本轮只运行本地 Fixture/Community 与安全/契约测试。未运行新 Live、Enterprise、Performance，未消费模型预算；不改变历史 failed 卡或 V2 待独立盲评状态，不宣称 7 个历史失败案例已在真实模型下修复。

决策输入中的任务识别、候选完整性和证据语义关联仍需模型与独立语义验证；结构约束不能自动证明因果，真实但语义无关的引用仍是残余风险。不得将本地通过当成 Live 质量通过。详见 [ADR-033](../../../../docs/adr/ADR-033-attribution-decision-rules.md)。
