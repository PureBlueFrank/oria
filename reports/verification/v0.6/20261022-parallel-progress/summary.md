# Oria 进展与并行实施验证

- 开始：2026-10-22；收尾：2026-10-23（Asia/Shanghai）。
- 任务：V0.6-T03 的派发接纳契约、显式重试/到期处理，以及进展/TODO 与证据索引同步。
- 起始提交：`476b034`，起始工作区干净。GitHub 已核实 PR #5 合入 main 的 `cbc486a5`；该 main tree 与本轮起始 tree 一致，本地旧 main 引用不代表远端状态。
- 结论：本轮内部原语与本地契约验证通过；V0.6-T02/T03/T04/Core 尚未关闭。

## 项目进展与本轮改动

V0.1–V0.3 Core 和必需 Live 已通过。V0.4 已收口，保留原 DeepSeek failed 卡与后续 GPT-5.6 Sol 推荐卡。V0.5 Core 完成，T07 Live 仍停在 29/80，剩余 51 次和独立盲评未执行。V0.6 T01 已有本地真实 PostgreSQL 卡，T02/T03 持续实施，T04+ 的完整 worker/可靠通知/验收仍未交付。

本轮并行实现了两组产物，由第三个 agent 独立复审。两个实现 agent 用量受限后，主助手完成回归、修复、整合与文档收尾：

1. **外部接纳契约**：稳定键、完整 operation 与 typed payload 绑定，独立 admit/lookup、当前权威租约检查及 unknown 的纯对账投影。类型/Protocol 不构成生产接收端；现有精确内存 Mock 限制保持，详见 [ADR-040](../../../../docs/adr/ADR-040-fenced-dispatch-admission.md)和[契约卡](../20261022-t03-dispatch-contract/summary.md)。
2. **显式调度原语**：schedule_retry/release_retry/expire，以当前授权与完整 CAS 保护一次性预算、取消优先、超时、状态/事件原子提交及最后 COMMIT 检查。没有自动 worker、Graph、业务重投或撤权后的特权清理，见[调度卡](../20261022-t03-job-scheduling/summary.md)。
3. **进展和证据**：更新 ROADMAP 的当前顺序与 TODO 表、详细路线本地资料、ADR/指南及统一证据索引；补齐此前缺失的草案、launch 审批、券 reservation 和 Mock 派发索引。

## 验证与失败历史

| 检查 | 结果与范围 |
| --- | --- |
| 调度定向回归 | 36 passed，真实临时 SQLite，包含竞争、撤权、事件与提交失败。 |
| 派发最终契约 | 47 passed，无 warning；包括独立复审发现的 11 项接缝回归。 |
| 既有 Mock 派发边界 | 初次修复阶段契约 44 + 既有派发 20 = 64 passed；完整 Community 也覆盖原派发限制。 |
| 完整本地 Community | `community.log`：1485 passed / 9 deselected / 5 既有 warnings，536.26 秒；只允许本机回环，不运行 Live/Enterprise/Performance。 |
| 最终覆盖口径 | 完整集启动后，仅新增派发契约中 3 项严格类型回归并修复该独立模块；最终 47 项契约全通过，最终 collection 为 1488/1497（9 deselected）。这是完整集与最终定向检查的联合覆盖，不冒称一次全量跑过 1488。 |
| 静态检查 | `format-final.log` 393 文件格式；`ruff-final.log` 全库 Ruff；`mypy-final.log` 196 源文件，均通过。 |
| 打包与 smoke | `build-final.log` wheel/sdist 构建通过；`package-content.json` 确认五个新/改源码与 wheel 字节相同；`smoke.log` CLI 0.1.0。 |
| 独立复审 | [报告](independent-review.md)：两项发现均已修复，最终 11 passed / 36 deselected / 无 warnings，无遗留阻断。 |

初次 uv 访问默认 cache 被沙箱拒绝；替换临时 cache 后缺 editable 构建依赖且 DNS 不可用，未开始测试。后续直接使用项目已有 `.venv`，未绕过锁文件安装业务依赖。首次 lint 的导入排序、长行及测试 lambda 写法失败保留于 `lint-initial.log`。离线构建因隔离 cache 缺 hatchling 失败（`build.log`），通过 uv 获取 pyproject 指定构建依赖后构建成功（`build-retry.log`），最终源码再次离线构建通过。两次接缝缺陷及序列化警告均有 RED/GREEN 日志，未删除失败历史。

最终代码/测试指纹见 [source-snapshot.json](source-snapshot.json)。基线 PR/push CI 为成功，见 [baseline-ci.json](baseline-ci.json)；它不证明本轮新改动的 CI。本轮提交推送后的 Actions 以交付消息中该提交的链接和最终状态为准。

文档检查覆盖 261 个本地链接，均存在。暂存区 diff 检查发现两份原始 pytest RED 日志自带行尾空白；为保留原始失败证据未清洗日志，排除这两份日志后其余暂存内容的 whitespace 检查通过。

## 后续 TODO 与未验证范围

1. 实现真实接收端的权威接纳/持久幂等/对账，并覆盖其余领域 fencing；每个真实 Adapter 独立验收。
2. 接入 Graph、自动调度、飞书会话及审批/外部事件恢复；现有 HTTP 仍只受理 Job。
3. T04 独立 worker 与 accepted pointer/orphan 生命周期，T05 可靠通知/SSE/Webhook。
4. 真实飞书的新拒绝/详情/分页，以及 PostgreSQL 新 revision、S0–S5 和两独立 worker S1/S2。
5. V0.5-T07 的剩余 Live 对照需显式执行决定、目标和预算，未自动续跑。

本轮未运行 Live、Enterprise、Performance、新 PostgreSQL 或真实飞书，未修改用户数据、依赖、migration、环境变量或安全边界。Fixture、Community 与外部验收分开记录。
