# Open Code Review 与提交前验证（2026-10-22）

## 范围与方式

按用户要求审查全部待提交代码，并补查既有 `main` 合入目标 `master` 时带入的代码。使用 `open-code-review v1.12.13` 的 `delegate preview/rule --format json` 做文件选择和规则解析，由当前开发助手逐项执行审查；未调用额外 LLM 服务或子代理。

- 工作区：133 个文件全部有处理记录；OCR 默认可审查 43/43，另补审默认排除的测试及文档。
- 既有 `main` → `master`：218 个文件全部有处理记录；OCR 默认可审查 90/90，另补审测试。
- 两个范围去重后共 54 个源码/测试/脚本/前端文件，语义审查覆盖 54/54；其余历史日志、图片和非代码证据的跳过理由逐项保存在 [review.json](review.json)。OCR 默认文件覆盖率不等于所有素材均做了代码语义审查。
- 审查基线为 `02261a5`，目标 `master` 基线为 `c57d59e`。远程 `main` 后续 `28b86be` 仅删除 README 的六行演示说明，提交后合并时保留该编辑；没有业务代码差异。

## 发现与修复

1. **Medium / bug，已修复**：回放页 `?scene=0.5` 使用非整数数组下标，导致 `render()` 抛出 TypeError。现在只接受整数并约束范围，其他值回到首幕。Node VM 执行实际内联脚本及真实回放数据，先复现失败，再验证空值、整数、负数、越界、小数、NaN、Infinity 七种 URL；前后证据见 `replay-before.json` / `replay-after.json`。
2. **Medium / documentation，已修复**：飞书指南仍称外部 T02 验证阻塞全部 T03 实施，部分入口和架构说明仍把已实现的 Job 受理/租约写为待实现。已同步当前契约和能力边界，未放宽任何完整验收门禁。

没有剩余 Critical/High 或其他阻断发现。逐文件覆盖、规则、源码摘要和发现字段见 `review.json`、两个 preview 及 `resolved-rules.json`。

## 验证结果

| 检查 | 结果与证据 |
| --- | --- |
| 格式 / Ruff / mypy | 381 文件格式、Ruff 与 190 源文件类型检查通过，见 `lint.log`。 |
| 完整 Community 集 | 首次 1227 passed、1 failed、9 deselected、5 warnings；唯一失败为沙箱拒绝 `127.0.0.1` 的 `socket.bind`，不是业务断言失败。原始记录保留在 `community.log`。 |
| 回环用例复验 | 仅为失败用例开放本地回环后 1 passed / 1 既有 warning（8.97 秒），见 `loopback-recheck.log`。无代码或测试改写，全部 1228 个被选中的不同用例均已有通过证据。 |
| Scenario A Golden | 45/45，通过，见 `scenario-a-golden.log`。 |
| RAG Golden | 60 条 × 3 管线，原门禁通过，见 `rag-golden.log`；不是 Live 模型或新性能验收。 |
| 打包 / CLI | wheel、sdist 构建及 CLI smoke 通过；两包均含 Apache 2.0 原文及 `License-Expression: Apache-2.0`，新增 Job 模块与源码一致，缓存未打包。见 `build.log`、`package-checks.json`、`smoke.log`。 |
| 演示 URL 回归 | 先复现小数页码异常，修复后实际脚本七种 URL 均通过。 |
| 文档 / 内容 | 九个当前入口文档的本地引用有效，`git diff --check` 通过；90 个 JSON 可解析，332 个范围内文件的常见凭证模式扫描无命中，见 `content-scan.json`。模式扫描不等于完整秘密审计。 |

## 边界与远程验证

本次没有新 Live Provider、真实飞书、新 PostgreSQL revision、用户数据库迁移或生产执行。新带理由拒绝/详情分页、新表 PostgreSQL 与实际 Job runner/领域副作用 fencing 仍按各自任务单独验收；T02/T03/Core 未因本轮提交关闭，V0.5-T07 Live 仍为 29/80 暂停。

本报告记录提交前事实。推送及合并后的所有 Actions 结果以对应 PR Checks 和目标 `master` 提交的 Actions 记录为准，不预填远程 CI 结论。既有报告的失败历史和源码快照保持原样；本轮摘要仅用于当前审查范围，不覆盖旧证据。

本轮 `community.log` 的仓库副本仅移除 pytest 堆栈中三处行末空格以满足差异检查；失败堆栈和结果均保留，未经处理的原始输出仍在本地 `.artifacts/ocr-20261022/community.log`。

## 首轮 CI 发现与打包门禁补验

首次推送 `e9cf37a` 的 [push CI](https://github.com/PureBlueFrank/oria/actions/runs/39725854351) 和 [PR CI](https://github.com/PureBlueFrank/oria/actions/runs/39725896587) 均在 `verify_t03_wheel.py` 失败：脚本仍要求 `platform_0011`，但本次包已包含 `platform_0013`。本地包内容/导入检查此前没有执行完整的安装后验证脚本，属于本次迁移配套检查遗漏；不将该失败归为基础设施问题，也不修改历史失败结论。

已在新建隔离虚拟环境中安装当前 wheel，先用旧断言复现相同错误（`package-regression-before.log`），再仅更新期望 revision，保持安装路径、资源完整性、重复初始化和表结构检查。环境依赖通过项目规定的 `make sync` 按 `uv.lock` 离线同步；首次工作区缓存缺少 grpcio，改用既有 uv 缓存成功，两个同步记录均保留，未绕过锁文件安装依赖。

修复后完整执行安装包门禁：T02 全模块导入、T03 资源/幂等初始化、T04 Provider、T05 RAG、T06 Tool、T07 Graph、T08 Demo、V0.2-T05 评测资源及严格类型消费，**9/9 通过**，见 `package-gate-after.log`。此修复不更改业务代码；无需重复已通过的本地业务全集，新提交仍须重新通过远程 CI。

该脚本已另行执行 OCR preview/rule 和完整语义审查，**1/1 通过**，见 `ci-package-review.json`。加上首轮 54 个不同代码文件，累计审查 55 个；没有剩余阻断发现。
