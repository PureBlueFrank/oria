# V0.6-T03：Job launch 审批准备

2026-10-22，Asia/Shanghai。基线 `517c012db326dfa2fd527c39c8be21bb102abfa3`，master clean（相对 origin/master 已有两个提交，保留）。实施前决策见 [design.md](design.md)，语义审查见 [review.md](review.md)。

## 交付

新增显式内部 `JobExecutionService.request_launch_approval(token, args, plan)`，把已持久化 Job 草案连接到真实 planned saga 和 pending launch approval。固定 typed 券参数与完整 LaunchPlan，审批 ID、saga 和逻辑 checkpoint 由 Job 派生；调用方不能指定 checkpoint、expiry、Context 或 adapter。当前 requester/独立 worker/owner、实际 campaign draft/launch 和既有审批申请 Policy 均须通过。审批有效期为服务端当前时间起最多一小时，受 Job timeout 限制。

先在平台快照锁内以业务单事务校验原始完整草案、成功 draft receipt 和计划，创建固定 planned saga；再重新获取两库写锁、重新核验全部事实，提交 Platform approval + 脱敏 audit/outbox。第二阶段业务事务只持 no-op 锁，平台提交完成后回滚释放，不在平台提交后继续写业务。阶段间中断可留下无审批的 planned saga，有效重试补齐同一审批；平台提交后响应丢失时重试返回原审批，不重复审计或延长 expiry。

重复/跨 epoch 仍重新授权和校验当前事实；过期、失效、拒绝或绑定/policy version 冲突的历史失败关闭，不新建替代审批。决定继续复用既有 ApprovalService/SOD，测试覆盖从 Job 草案申请→独立审批人批准→既有券 reservation。await 后及 Business/Platform COMMIT 入口分别检查当前目录/Policy/lease，平台提交还检查审批绑定/期限。

原子性限于各自数据库事务，不是跨库 2PC。COMMIT 交给驱动后的外部撤权、时钟变化或 cancellation 不能保证追溯撤销；已提交 saga/审批必须按稳定 ID 重试读取。逻辑审批点不是 saver checkpoint，本入口不改变 Job 状态/accepted pointer、不预留或执行券、未装配到 API/Graph/飞书/worker。

## 验证

- 新增 45 项真实临时 SQLite/合成身份测试：有效申请与 SOD、重复/跨 epoch、审批历史/计划/业务事实绑定、权限与租约撤销、expiry 上限、不延长历史有效期、阶段间中断、审批/audit/outbox 整体回滚、丢失提交响应、await/cancellation、两种 COMMIT hook、阶段间事实变化。
- 两独立 DatabaseResources/engine 实例直接观测实际 Job cancel/takeover UPDATE 和业务 publication UPDATE 到达数据库并被锁阻塞；过期旧 worker 审批整体回滚，新 epoch 可补齐；取消或业务变更在提交后生效，随后重试拒绝。这不是两个独立 worker 进程、PostgreSQL 或 S2 证明。
- 相关四文件回归 **182 passed / 1 既有 warning**，114.31 秒：[06-targeted.log](06-targeted.log)。
- 全库格式/Ruff、192 源文件 mypy 通过：[08-lint-corrected.log](08-lint-corrected.log)。
- wheel/sdist 构建通过：[10-build.log](10-build.log)。新隔离 venv 按 uv.lock 导出的 requirements 离线安装，wheel 新接口 `-I` 导入和 CLI smoke 共六条命令全部通过：[package-results.json](package-results.json)。
- 八个 wheel verifier 与 strict wheel consumer 共九条命令全部退出 0：[wheel-verifier-results.json](wheel-verifier-results.json)；sdist 不含本地缓存/虚拟环境，两个受影响源文件与 wheel 逐字节一致：[wheel-source-match.json](wheel-source-match.json)。完整 Community **1385 passed / 9 deselected / 5 既有 warnings**，pytest 485.98 秒，命令 487.99 秒、退出 0：[09-community.log](09-community.log)、[community-result.json](community-result.json)。

所有 uv 命令使用 `UV_CACHE_DIR=.artifacts/uv-cache UV_OFFLINE=1 UV_NO_SYNC=1`；没有变更依赖或个人环境。完整 Community 因含临时回环 HTTP 端口而在允许本机端口的环境执行。代码/锁指纹见 [source-snapshot.json](source-snapshot.json)。未执行 Live/Enterprise/Performance、新 PostgreSQL、真实飞书或用户库迁移。

## 失败历史

| 证据 | 实际结果 | 解释 |
| --- | --- | --- |
| [01-valid-red.log](01-valid-red.log) | 1 failed | fail-closed scaffold 在有效申请时拒绝，行为 RED |
| [02-valid-green.log](02-valid-green.log) | 1 failed | 测试误用 outbox topic，修正为既有 approval 状态 topic/created event 关联 |
| [03-bindings-boundaries.log](03-bindings-boundaries.log) | 13 failed / 16 passed | 测试夹具错误：Policy 方法/engine 属性名、child model 序列化及测试代码块位置；修正夹具 |
| [04-bindings-corrected.log](04-bindings-corrected.log) | 29 passed | 有效链路、绑定、中断与异步边界通过 |
| [05-races-commit.log](05-races-commit.log) | 2 failed / 43 passed | 平台 COMMIT 探针误命中前置授权审计；增加本次 approval 事务标识后只检查目标提交 |
| [07-lint.log](07-lint.log) | 退出 2 | 一处 SIM117，合并相邻 async with，未放宽规则 |

宿主使用 open-code-review-delegate 审查当前改动及前两个提交，12 个不同源文件/测试全部检查；OCR 自生成预览元数据明确跳过，逐项覆盖见 [review.json](review.json)。未声称独立复审。T02/T03/T04/Core 未关闭，V0.5-T07 29/80 不变；下一优先项为外部 dispatch/其他领域 fencing，然后受控 Graph 调度与等待恢复。

## 提交与远程验收

本轮按此前授权，在本地验证与 OCR 宿主审查通过后提交，并以 PR 合入 master；GitHub Actions 最终状态以关联 PR 与交付回复中的运行链接为准。没有在本卡编造尚未发生的远程验证结果。详细路线的本地资料同步更新且沿用仓库忽略规则。
