# 审批回放测试跨秒回归修复

2026-10-22；基线 master `f2c21f6`。本轮功能原 PR #3、分支 push、master push、并行 main CI 与 Pages 已通过；并行 master→main PR #4 的 Linux 回归出现一个既有测试失败（1384 passed / 1 failed）。原始运行：[39764832309](https://github.com/PureBlueFrank/oria/actions/runs/39764832309)，日志 [01-original-ci-failure.log](01-original-ci-failure.log)，六轮原始状态见 [prior-ci-status.json](prior-ci-status.json)。保留失败，不对旧源码重复重跑以选取通过结果。

原测试在所谓重复请求时重新调用签名夹具；签名时间戳取真实时钟，跨秒后与相同 nonce 生成不同 transport replay key。Receiver 合法新增第二条 ingress_replays，语义 receipt/审批决定/audit/outbox 仍只有一次；测试误要求 transport 行数完全不变。该测试在功能基线到合并提交之间未改变（review.json 保存核验结果）。

先用只影响测试签名夹具的可控时钟让每个新请求跨一秒，原断言稳定 RED：[02-deterministic-red.log](02-deterministic-red.log)，1 failed，精确复现 2 != 1。修复为同一请求原样重放，另显式验证相同动作/理由/nonce、不同签名时间戳的重试只增加一条 transport 记录，不重复决定/audit/outbox。不同理由仍 409 且整笔回滚。没有改动 production ingress、Policy 或账本语义。

70 项审批交互、事务受理、ingress receipt 和 Feishu 契约测试通过（29.27 秒，1 既有 warning）：[03-targeted-green.log](03-targeted-green.log)。全库 386 文件格式/Ruff、192 源文件 mypy 通过：[05-lint-corrected.log](05-lint-corrected.log)。首次格式失败保留在 [04-lint.log](04-lint.log)。只对一份测试文件作语义调整，OCR 默认排除测试，已显式补充宿主审查并保存规则/逐项结果：[review.json](review.json)。

新提交推送后的 CI 按仓库约定继续跟踪，最终链接随交付提供；T03/T04/Core、Live 29/80 与生产能力状态不变。
