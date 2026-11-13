# V0.6-T03 本地参考接收端：审批与领域事实接入

日期：2026-11-13（Asia/Shanghai）。基线 HEAD：541e3b7306eac14070a979ced8b235c9d5c03cf7，包含上一轮未提交的 Job 权威 bridge。本轮继续用户批准的本地参考接收端范围；未提交、推送或执行外部写入。

## 实现

新增 tests/integration/approved_coupon_reference.py，使用实际 MaterializeCouponBatchArgs、LaunchPlan 和审批 ID；只在独立临时 SQLite 接收端记录合成历史，不调用券 Adapter、不推进 Oria ledger/coupon/saga、不发送网络请求。

- 复用 JobExecutionService 的 SQLite/仓储绑定约束；接收端文件不能是 platform/business 数据库。
- 重载真实临时 Job、当前身份目录、独立 worker 与实际 campaign 资源 Policy；限制 Job 派生 campaign/coupon 标识。
- 在 Job 完整快照平台锁内读取真实审批，复用 approval_resume_rejection 校验 approved 状态、动作、工具、plan hash、checkpoint、policy version、期限，同时约束 requester 与职责分离及不支持的 business binding。
- 复用 JobCouponDispatchWriter.load 获取业务写锁并校验真实草案版本、完整计划、saga 与 committed reservation；仅接受 reserved，拒绝混用既有真实/Mock 派发历史。
- 平台/审批锁与业务事实锁共同持有至接收端提交结束；接收端入口及最终 COMMIT 边界重查当前身份、Policy、租约与审批期限/version。参考 receiver 增加受保护的 typed payload 校验方法，原简单合成 payload 行为保留。
- 稳定 operation key 只含券资源和固定操作版本，不含 payload hash、Job epoch；完整 operation 绑定仍检查 hash/Job/资源。lookup 同样走完整授权/事实检查。
- 接收端实例退出作用域即禁用；业务事务只持 no-op 锁后 rollback，只有独立合成历史持久化。这不是跨三库原子事务。

## 验证与失败历史

新增 27 项用例覆盖有效审批、幂等与跨 epoch 查询、缺失/失效/错误 requester/checkpoint/hash/policy/过期审批、草案/saga/reservation 变更，以及历史查询不得绕过这些条件；提交前审批期限或 policy version 变化回滚新接纳并保留原历史。

独立 Python 进程在接收端入口及提交前，对实际 approvals 行和 coupon_batches 行执行 UPDATE：均收到 SQLite locked；接纳完成后同样的 UPDATE 均成功。每次验证确认 Oria execution ledger、券/saga 及事件计数保持不变，Mock materialize 调用为零。

首次 [initial.log](initial.log)：25 passed / 2 failed。两项失败发生在新测试准备自批数据时，数据库既有 CHECK (decider IS NULL OR requester <> decider) 正确拒绝，接纳代码尚未运行。没有绕过数据库职责分离；改为有效数据库记录但 requester 不匹配 Job 的接纳拒绝测试。首轮 [lint-initial.log](lint-initial.log) 两条长行失败，格式化修复。

最终组合测试及静态检查结果见本卡末尾；保留所有首轮记录。组合覆盖新增审批参考接纳、上一轮真实 Job bridge、持久参考 receiver、dispatch 契约及既有 Job 租约/Mock 派发/对账。本轮仅新增测试支持及扩展测试支持方法，不改 src、公开 API、迁移或依赖，因此不重复完整 Community、build 或 smoke。

## 能力边界与下一步

这是临时 SQLite + 真实本地身份/Policy/审批/领域仓储 + 合成接收端效果的 Integration/Security/Recovery 证据。身份与审批数据仍为合成测试数据，既有业务 Adapter 精确类型限制不变。该 helper 不实现生产 FencedDispatchAdapter，不在 Runtime/HTTP/Graph 中装配，不可把返回的合成 succeeded 当作真实券已派发。

本地参考接纳所需的 Job/审批/领域事实边界已接通并验证；后续需明确产品代码封装与真实目标认证，再接收端结果到 Oria ledger/领域投影的对账、其他领域 fencing、Graph/事件恢复及 worker。没有网络目标、真实企业接入或 PostgreSQL S2 验收；T03/Core 不关闭，V0.5-T07 保持 29/80。COMMIT 交给驱动后的外部撤权或期限变化不保证追溯回滚。Live、Enterprise、Performance、真实飞书、用户库迁移与新 CI 均未运行。

## 最终结果

[combined.log](combined.log)：**182 passed / 1 既有 Starlette warning**，97.12 秒，退出 0。[lint-final.log](lint-final.log)：403 文件格式/Ruff、197 源文件 mypy 通过。受影响文档本地链接和 git diff --check 通过。源码指纹见 [environment.json](environment.json)。没有新远程 CI；所有改动保留在本地。
