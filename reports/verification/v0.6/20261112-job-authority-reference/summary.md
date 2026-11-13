# V0.6-T03 参考接收端接入真实本地 Job 权威

日期：2026-11-12（Asia/Shanghai）。基线：541e3b7306eac14070a979ced8b235c9d5c03cf7。本轮用户要求先推送已有恢复增量，再继续开发；本卡的新代码未提交、未推送。

## 实现与范围

新增 tests/integration/job_authority_reference.py，仅作为测试支持，不注册到 Runtime，不实现 FencedDispatchAdapter，也不接入现有 Mock 券派发入口。

execute_reference 从真实临时 JobRepository 经 JobLeaseService 重载当前 Job、身份目录和独立 worker Policy。绑定 campaign 场景、Job 派生资源及 token；拒绝其他租户、worker 和场景。使用完整 Job 快照 CAS 的 _hold_execution_lease 持有平台 SQLite 写锁，直到独立接收端数据库提交结束。参考 receiver 的 _authorize 覆盖合成表读取，以真实 guard.check_commit 与锁内 Job 检查为准；不复制权威到接收端。作用域结束即禁用实例，禁止 replace_authority 写入合成替代权威。

参考效果仍只是 history 唯一行；没有券状态变化或外部系统调用。同步本地接纳期间不引入 await/线程/网络；锁内时钟与目录/Policy 在提交前再次复核。拒绝审计通过现有 _write 在平台锁释放后处理。

## 验证

- 新增 15 项 integration/security/recovery 用例。组合运行新增测试、真实 Job 租约测试、持久参考 receiver 和 dispatch 契约：**103 passed / 1 既有 Starlette warning**，39.94 秒，见 [combined.log](combined.log)。
- 同 worker 服务重建、跨 epoch 历史查询仍返回原确定结果，只保留一次合成效果；Job 本身不被接纳修改，接收端 authority 表为空。
- 新接纳和历史查询均覆盖最终提交前身份撤销、Policy 撤销及租约到期；新增历史回滚，原历史保留，Job 状态不变。
- Job 读取后、获取锁前取消并更改 revision，完整快照 CAS 拒绝且不创建接收端库。
- 在接收端入口及最终授权检查时启动独立 Python 进程，对实际平台 jobs 行执行 UPDATE（timeout=0）：两次均数据库锁拒绝；接纳完成后同一 UPDATE 成功，随后旧 epoch 拒绝。该证据是 SQLite 跨进程写锁边界，不是生产 worker 或 PostgreSQL S2。
- 平台锁释放后注入响应丢失，新服务只读查询可取回已提交结果。接收端提交不随平台后续异常回滚，不声称跨库原子性。
- worker/resource/tenant/scenario 错配均拒绝；逃逸 receiver 在锁释放后不可再使用，不能替换权威。
- [lint.log](lint.log)：全库 401 文件格式/Ruff、197 源文件 mypy 通过。

首次 [initial.log](initial.log) 为 1 failed / 9 passed：新测试错误地期待底层 JobConflict，实际契约正确抛出 DispatchRejected；修正测试预期，没有放宽生产拒绝。后续增加历史路径及进程锁/丢失响应场景后执行最终组合验证。日志只替换工作区绝对路径并清理行尾空白。

仅新增测试支持与测试，生产 src/公共契约/依赖/迁移均无变化，未重复全量 Community 或构建；既有 63 项参考恢复与 dispatch 契约已包含在本轮 103 项组合内。未运行真实飞书、PostgreSQL、Live、Enterprise、Performance，无用户数据操作。

## 尚未完成

本卡只接入真实本地 Job + 当前身份/Policy + 租约锁。launch 审批、当前领域事实与最终副作用边界尚未接入此参考 bridge，因此它不能授权券派发。下一步仍是补齐这些固定领域约束，再考虑生产接收端封装；不得将测试 bridge 当作网络 Adapter。其余领域 fencing、Graph/审批事件恢复、独立 worker 和可靠通知继续待办。T03/Core 未关闭，V0.5-T07 仍为 29/80。

已有恢复增量提交 541e3b7 已推送 main，本卡增量不在该提交中；远程 CI 的最终状态单独跟踪，不以本地 103 项结果替代。

## 已推送基线 CI 最终状态

提交 541e3b7306eac14070a979ced8b235c9d5c03cf7 的全部工作流已结束：[CI](https://github.com/PureBlueFrank/oria/actions/runs/43250249749) 核心测试、quality、package、eval-golden、两项 extras-smoke 成功；arm64-smoke 未执行任何步骤即取消，annotation 为 `The configuration 'macos-14-arm64-us-default' is not supported`。CI 总结 cancelled，属于 runner 基础设施阻塞，不能记全绿。[Pages](https://github.com/PureBlueFrank/oria/actions/runs/43250249390) 成功。本卡新增 bridge 仍未推送，不属于上述远程测试范围。
