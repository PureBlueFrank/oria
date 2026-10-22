# T03 Job launch 审批准备：实施前决策

基线 `517c012`，master clean；已有两个本地提交保持原样。本切片补齐 Job 草案到真实持久审批/saga 的前置链路，仍不实现 Graph 或外部 dispatch。

内部固定入口 `request_launch_approval(token, args, plan)`：只接受既有 typed 券参数与完整 LaunchPlan，不接受调用方 checkpoint、expiry、审批 ID、Context 或 adapter。复用当前 requester/独立 worker、实际 campaign draft/launch Policy，另检查既有 `approval:launch:request`。所有路径（包括历史与跨 epoch）重新检查当前身份、owner、lease、未取消、原始草案与成功 draft ledger。

两库不做 2PC。第一阶段在平台完整快照写锁内，业务事务校验当前完整草案/计划并创建固定 planned saga；COMMIT 入口检查目录/Policy/lease。saga ID 和逻辑 checkpoint 由 Job 派生，与 epoch/accepted saver pointer 无关。第二阶段重新取得平台锁和业务写锁，重新验证全部事实及固定 saga，再通过同一平台事务创建真实 pending approval + 脱敏 audit/outbox。平台 COMMIT 入口重查授权/期限；业务锁保持到平台提交完成后回滚 no-op，平台提交后不再执行业务变更。

阶段间故障可以留下无审批的 planned saga，不能产生执行授权；同一有效 Job 重试补齐审批。平台已提交后响应失败，重试读取同一审批而不延长 expiry、不重复审计。固定审批 ID 绑定完整 plan/checkpoint/requester/policy version；过期、拒绝、失效或冲突历史均失败关闭，不悄悄新建审批。新审批有效期为服务器当前时间起最多一小时且不超过 Job timeout。审批决定继续复用现有 ApprovalService 与职责分离；本入口不决定审批，不改变 Job 状态/accepted pointer，不预留或执行券。

临时 SQLite/合成身份验证：有效申请→独立主体批准→既有券 reservation；重复/跨 epoch；计划/身份/租约/策略/历史冲突拒绝；阶段间故障及响应丢失恢复；事务 await/COMMIT 撤权和到期回滚；两独立资源实例竞争观测实际 UPDATE。运行相关行为回归、全库静态与 Community，保留失败日志。没有用户库、Live/Enterprise/Performance、PostgreSQL、远程写入或新依赖/migration。
