# V0.6-T03 本地持久参考接收端恢复验证

日期：2026-11-12（Asia/Shanghai）。基线：d194721e343454aa53334a3116db01e9a3479da0。用户已选择本地持久化参考接收端，本轮继续该范围；未提交、推送或执行外部写入。

## 实现

仅修改测试支持接收端及其测试，不接入生产 Runtime/Job 派发，不新增依赖、生产 schema 或用户库迁移。

- 参考接收端增加可注入时钟，在事务开始及最终同步 COMMIT 边界复核合成权威与当前租约，历史返回和 not_found 同样复核。
- 显式 BEGIN IMMEDIATE/commit/rollback，并在所有路径关闭 SQLite 连接。权威写入与接纳共享同库写锁；合成效果就是唯一 history 行，没有外部副作用。
- 限定 fixture-coupon/materialize_coupon_batch/schema v1，拒绝其他接收端、工具或 schema；保留稳定键及完整绑定校验。

## 验证与失败历史

- [clock-red.log](clock-red.log)：新增接纳、历史查询两条事务中途到期回归均先失败（DID NOT RAISE），确认原参考实现只有入口检查。
- [clock-green.log](clock-green.log)：修复后 4 passed（含原有两项）。
- [contracts.log](contracts.log)：最终参考接收端 16 项 + 原 dispatch 契约 47 项，共 **63 passed**，13.22 秒。
- 使用独立 Python 子进程在 commit 前 os._exit(71)，新进程 lookup 为 not_found，纯对账投影仍为 unknown，不能据此重投；commit 后 os._exit(72) 不返回响应，新进程仍查到 succeeded。分别确认 history 行数为 0/1。
- 两独立子进程竞争接纳，返回同一历史且只有一行效果；主进程持权威写事务更新 epoch，已就绪子进程随后提交旧请求必须拒绝。通过管道协调，不依赖 sleep 排序；不声称确定测量了阻塞时长。
- 注入 commit 异常，验证回滚、连接关闭及无新历史；覆盖未知 target、版本、租户、撤权、取消、过期及历史访问拒绝。
- [lint-initial.log](lint-initial.log)：首轮新增测试长行失败，格式化修复后 [lint.log](lint.log) 全库 399 文件格式/Ruff、197 源文件 mypy 通过。

未重跑完整 Community/打包：本轮不改 src、公共契约或发行包，定向覆盖受影响测试支持模块及其 dispatch 契约。未执行 Live、Enterprise、Performance、真实飞书或 PostgreSQL。

## 边界与后续

这是 Fixture/Contract + 本地 SQLite Recovery/Security 证据。授权仍为可信测试控制端设置的布尔值，时钟默认合成时间；replace_authority 仅为测试控制接口，不是生产租约变更 API。没有网络认证、真实 requester/worker Policy、审批/领域事实或真实 Job 权威集成。操作系统进程退出恢复不是断电持久性证明；COMMIT 已交给驱动后的期限变化不保证追溯撤销。

下一优先项仍是受信身份/审批/领域事实与真实 Job 权威的集成边界及其余领域 fencing，再接 Graph/审批事件恢复。独立 worker、可靠通知、PostgreSQL S2 和 T03/Core 不因本卡通过而关闭。V0.5-T07 仍为 29/80。

上次推送 d194721 的 CI 已结束：核心测试、quality、eval、extras、package 通过；ARM64 因 GitHub runner 配置不受支持取消，CI 总结为 cancelled，Pages 成功。本轮不将其改记为全绿，也没有新远程 CI。
