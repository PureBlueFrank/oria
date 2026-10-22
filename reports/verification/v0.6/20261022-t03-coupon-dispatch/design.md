# T03：受控 Mock 券派发与结果提交，实施前决策

基线 main `30dd256`；开发分支从 main 派生，保留式合入已审查/CI 通过的 `020ed13`。后续 PR、合并与交付目标均为 main。

本轮实现内部 `JobCouponDispatchService.materialize(token, args, plan, approval_id)`。构造器只接受既有 InMemoryCouponBatchAdapter 的确切类型、SQLite 双库和 0–5 秒内的超时；没有网络 Adapter、HTTP/Graph 默认装配、自由 tool、Context 或调用方 callback。它完成 Community Mock 纵向链路，不宣称真实外部派发已 fenced，不修改既有非 Job Service 的语义。

仅消费已经存在的、固定 Job execution ID 的券 reservation。当前 requester/独立 worker/owner、实际 campaign draft/launch Policy、完整计划与真实 approved approval 均重新校验，包括重复和跨 epoch。必须是 Job 新入口创建的固定 planned saga；逻辑 checkpoint 不作为 saver checkpoint。先在完整平台快照锁内，以 Business 单事务把 reserved 持久化为 executing，并记录 admitted 事件；提交成功后才可能调用 adapter。执行前重新获取业务写锁并复核全部事实，持平台/业务锁经过短暂 Mock 调用及结果事务；此锁内仍不允许真实网络/模型/人工等待。

在真正 await adapter 之前同步复核身份、Policy、审批与有效 lease。Mock 返回 accepted 且提交门禁仍有效时，同一 Business 事务提交 coupon draft→materializing→ready、saga planned→coupon_materialized、ledger succeeded、Domain/Audit/outbox。结果为 rejected 则 failed，为 unknown/异常/超时则 unknown，不改变券和 saga。不保存原参数或异常文本，事件仅含 ID、epoch、hash 和状态。

若调用后撤权、过期、task cancellation 或结果写入/响应异常，executing 已经持久，结果事务可以回滚而不能把执行伪装回 reserved。后续有效调用在取得相同平台锁后观察到遗留 executing，将其标 unknown 并进入对账，绝不再次调用 adapter。平台锁串行化受控派发；进程退出释放锁后同样失败关闭。unknown/failed 历史不重投；成功历史必须核验 ready coupon、进展后的 saga 与原始完整草案/计划/成功 receipt，仍受当前权限和审批限制。不自动对账、补偿或恢复 Graph。

await 后和两阶段 COMMIT 入口检查当前目录、Policy、审批期限与租约。平台/业务锁阻止其他 DB writer 越过调用区间，原始目录/Policy 外部撤权及驱动已接收 COMMIT 后的时间变化仍不具备跨系统追溯撤销语义。两库不是 2PC。SQLite 暂停进程持锁会阻塞其他 writer，只适用于 Community 单 worker；不声称 PostgreSQL、S2 或独立 worker 接管验收。

验证：有效全链路、重复/跨 epoch 与旧 token；失败/unknown/异常/超时；执行前与调用后撤权、expiry、取消；COMMIT 入口与事务故障；遗留 executing 的未知收敛；成功历史事实篡改；独立 SQLite 资源的实际 UPDATE 竞争。保存 RED/GREEN、必要回归、全库静态/Community、安装包与 OCR 宿主审查证据。无新依赖、migration、环境变量、真实通道/用户库或 Live/Enterprise/Performance。
