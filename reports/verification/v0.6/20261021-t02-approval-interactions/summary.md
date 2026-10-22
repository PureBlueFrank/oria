# V0.6-T02：带理由拒绝与审批详情/翻页

日期：2026-10-21，Asia/Shanghai。任务依赖：V0.5-Core、V0.5-T03。本卡记录本地实现及 Fixture/Community/Security，**不关闭 T02 总门禁**。真实飞书新增交互、本 revision 的 PostgreSQL、Live/Enterprise/Performance 均未执行。

## 实现和证据范围

- 拒绝复用现有 ApprovalService 和 receiver 的唯一事务；receipt/replay、完整审批快照 CAS、决定审计和 outbox 原子提交。理由固定字段、非空、最多 1000 字，未知字段和非法类型拒绝；同一动作改变理由返回冲突，不修改原决定。
- 受信服务一次事务签发 approve/reject/view 三种 tenant、审批版本、身份、chat/message 和有效期绑定。数据库只存完整引用摘要；重建服务后可解析。三种引用共享随机前缀以复用原控件，不赋予额外权限，动作类型仍由对应的服务端记录决定。
- 每次详情/翻页重新检查当前身份、Policy、职责分离与持久绑定；严格整数页码只选择已经授权的展示。每页最多五段，查看不决定审批、不创建业务 outbox；重试不新增绑定。审批版本变更后隐藏旧决定控件，并提示重新发送当前版本卡片。
- 新投递从无控件占位、激活到终态均使用 JSON 2.0；拒绝表单提交 `rejection_reason`，终态移除决定控件。保留 JSON 1.0 golden、旧单独批准引用及原 receipt 内容摘要。新增卡片 golden 覆盖表单、第二页和拒绝终态，文本脱敏并转义标记，保留用户引号。
- 新增 `platform_0012`，未改写旧迁移，旧记录默认 JSON 1.0。升级保留既有引用；存在 reject/view 记录时降级拒绝，不删除数据。仅操作测试临时 SQLite，未迁移用户数据库。

详细边界见 [ADR-039](../../../../docs/adr/ADR-039-reasoned-approval-card-interactions.md) 和 [API 指南](../../../../docs/guides/api-foundation.md)。路线及证据索引同步了本增量；详细路线 V0.5 总览同时修正为已有的 T01–T06/Core 通过事实，T07 保持 29/80。

## 环境与可复现性

基线 `02261a59e46927eedfaf1a9518c282bd256f8276`，起始工作区干净；本卡为其上的局部未提交改动。Python 3.11.15、uv 0.12.6，macOS Darwin 25.6.0，x86_64 进程，使用现有锁定 `.venv`，没有安装新依赖。锁文件、源码、测试和卡片 golden 的 SHA-256 见 [source-snapshot.json](source-snapshot.json)。每项测试使用隔离临时配置，无单一共享 config/dataset/eval 指纹；本次不是模型质量评测，无真实模型 request ID。

本地命令显式设置 `UV_OFFLINE=1 UV_NO_SYNC=1`。受限测试使用可写隔离 uv cache；独立进程回环测试在获准后使用本机端口，不访问外部 Provider。最终命令分别保存日志：

| 检查 | 结果与证据 |
| --- | --- |
| 完整本地门禁 | **1151 passed / 9 deselected / 5 既有 warnings，352.76 秒，退出码 0**；[community-final.log](community-final.log) |
| 末次交互定向 | 59 passed；[targeted-after-review.log](targeted-after-review.log) |
| 格式/Lint/mypy | 369 文件格式、Ruff、183 源文件类型检查通过；[lint-final.log](lint-final.log) |
| wheel/sdist | 离线构建成功；[build-final.log](build-final.log) |
| 包内容与直接导入 | 10 个改动模块与源码一致，全部 platform 迁移 hash 通过；从构建 wheel 导入并验证 platform_0012/business_0011；[package-content.json](package-content.json)、[wheel-import.json](wheel-import.json) |
| CLI smoke | `make smoke`，0.1.0；[smoke-final.log](smoke-final.log) |

命令分别为 `make lint`、`make test`、`uv build --out-dir .artifacts/v06-t02-approval-interactions-20261021`、`make smoke`。定向集合包含 `test_v06_approval_interactions.py`、`test_v06_approval_delivery.py`、`test_v06_approval_cards.py`、`test_v06_transactional_approval_acceptance.py`、`test_v06_feishu.py`，扩展覆盖审批 API、安全、服务启动/重启和 PostgreSQL 契约。各轮不累加，不把 deselected 当作通过。

## 断言与历史

| 断言 | 直接或负面证据 |
| --- | --- |
| 理由约束与不变决定 | 真实 AES/签名合成 HTTP 拒绝、重建服务、相同请求重复、换理由 409；非法理由零受理记录 |
| 意图与权限不可伪造 | approve 引用不能携带理由改为 reject；决定动作不能携带页码；当前身份撤销拒绝；沿用原 tenant/chat/message/SOD/有效期安全用例 |
| 单胜者与恢复 | approve/reject 并发仅一个决定、一个 receipt、一次成功审计和 outbox；旧版本详情不重新暴露决定控件 |
| 详情无业务副作用 | 第二页实际授权投影、返回上一页、重复响应不新增绑定、非法页回滚、不改变 pending 或业务 outbox |
| 迁移与旧契约 | 新 revision 升降级、旧行/引用保留、新意图存在时拒绝降级；原 JSON 1.0 golden 不变 |

失败和中间结果完整保留：

1. 初次 uv cache 被沙箱拒绝，未执行测试：[regression-before.log](regression-before.log)。改用可写隔离 cache 后，新增八个用例均失败，复现原实现不支持拒绝/详情：[regression-before-tests.log](regression-before-tests.log)。
2. 新迁移格式化后 hash 清单未同步，完整性门禁失败关闭，26 failed / 25 passed / 22 errors；同步最终 hash 后 73 passed：[targeted-first.log](targeted-first.log)、[targeted-second.log](targeted-second.log)。
3. 扩展页码用例最初误用只允许更新决定字段的 repository.replace 修改业务绑定，1 failed / 17 passed；改为正常新增带业务绑定的合成审批，未放宽 Repository 不变量：[targeted-expanded.log](targeted-expanded.log)。
4. 扩展集合 108 passed，唯一失败为沙箱禁止 loopback bind；在允许本机端口后由完整门禁复验：[targeted-final.log](targeted-final.log)。
5. 首次完整门禁 1149 passed / 9 deselected / 5 warnings；随后补充引号/标记转义及旧版本详情恢复提示两个用例，末次交互定向 59 passed，最终全量另记：[community-first-pass.log](community-first-pass.log)、[targeted-verified.log](targeted-verified.log)、[targeted-after-review.log](targeted-after-review.log)。
6. 中间静态检查发现中文标点与测试行长，修正后最终检查通过；保留 [lint-review-adjustment.log](lint-review-adjustment.log)。首次构建见 [build-first.log](build-first.log)，末次源码调整后重新构建并核对当前包内容，不复用旧包结论。

最终新增 19 项用例，完整门禁通过 1151 项；第一轮 1149 与最终轮不累加。五项既有告警为 Starlette/httpx 弃用及 SQLite/Alembic 外键反射提示。日志仅脱敏用户/临时目录与行尾空白，未删除失败内容。机器可读范围与结论见 [checks.json](checks.json)。

## 等级与后续

Fixture、Community/Security 的本地结果分别由上述实际契约和 SQLite/独立进程证据支持。真实飞书新增 JSON 2.0 表单/分页未运行；PostgreSQL 新 revision、Live、Enterprise 和 Performance 未选择、未执行。10 月 18 日原真实批准卡和 10 月 17 日 PostgreSQL 卡只作历史范围引用，不能覆盖本增量。

未向外发送消息、开放公网入口、改动飞书权限、执行用户数据迁移、提交或推送；本次没有 GitHub Actions。T02 仍进行中，T03 依赖未关闭；V0.5-T07 29/80 保持原状。

下一步的目标、合成卡片内容、一次发送/更新、重启、分页/拒绝、30 分钟 Cloudflare 回调窗口及关闭条件，已具体整理为[待批准实测方案](real-validation-plan.md)。仅在取得外部写入和窗口的明确批准后执行。
