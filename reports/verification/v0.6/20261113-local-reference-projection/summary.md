# V0.6-T03 源码级持久本地参考接收端与结果投影

日期：2026-11-13（Asia/Shanghai）。基线：75fdd64b17da54a5d8961cc688e9380ac9cb55dc。用户要求先提交已验证的参考接纳增量到 main，再继续开发。本卡源码增量保留在本地，未提交/推送。

## 交付

新增显式 `JobCouponDispatchService.local_reference(leases, databases, path)` 和 `LocalCouponReferenceStore`，不再依赖 tests 模块才能使用。普通构造器仍仅接受精确 InMemoryCouponBatchAdapter；没有新增默认 Runtime/API/Graph 装配、环境变量或网络能力。只用于合成数据/隔离本地库。

复用现有服务的真实本地身份、独立 worker、资源 Policy、完整 launch 审批与领域事实检查。平台完整快照锁和业务事实锁覆盖接纳；同步 guard 在接纳前及参考库 COMMIT 前验证当前权限/租约/审批。先将 Oria ledger 持久化 executing，参考库记录稳定操作与确定合成 receipt，再由既有 writer 单业务事务更新 ledger、券 ready、saga、Domain/Audit/outbox。

接收端 schema v1 位于单独文件，只保存稳定键及脱敏观察，不存原始参数或凭证。拒绝 platform/business 路径及符号/硬链接别名，不修改外来 SQLite schema；SQLite 争用不等待（timeout=0）。lookup 不接纳、不重投，不存在历史文件时也不创建文件。

参考 receipt 用 local_reference_coupon、localref_ 与 synthetic_ 标识；它仅表示本地合成效果。接收端提交后响应丢失/协程取消/业务投影失败，原历史仍持久保留；新服务和新 epoch 可经 reconcile 读取确定回执并完成领域投影。unknown/not_found 只保持未知，不增加派发次数。其他接收端的终态 receipt 不作为参考终态读取。平台/业务/参考库不是分布式事务。

## 验证与失败历史

- 新增 14 项测试，结合既有 Mock 派发/对账共 [66 passed / 1 既有 warning](focused-initial.log)，48.06 秒。覆盖成功/重建/重复、响应丢失、取消、投影回滚、跨 epoch 对账、缺失历史不重投、提交前身份/Policy/租约/审批变化、领域拒绝、库别名和外来 SQLite 文件保护。
- [lint-initial.log](lint-initial.log)：首轮长行，格式化修复；[lint.log](lint.log)：mypy 发现新增分支 Receipt | None 注解缺失，补齐后 [lint-final.log](lint-final.log) 全库 405 文件格式/Ruff、198 源文件 mypy 通过。
- [build.log](build.log)：首次临时 uv 缓存缺 hatchling 且沙箱无法解析 PyPI，失败保留；使用现有 uv 缓存重试 [build-retry.log](build-retry.log) wheel/sdist 成功。没有绕过锁文件安装或变更依赖。
- [smoke.log](smoke.log)：CLI 通过。[wheel.log](wheel.log)：两个变更源码逐字节匹配 wheel，隔离 Python 导入确认使用 wheel 内代码和显式 reference 工厂。
- 本轮涉及源码派发与领域投影，执行完整 Community；最终结果见本卡末尾，不以定向测试代替全量。

所有本地运行使用 Python 3.11.15、uv 0.12.6 与现有 uv.lock，临时 SQLite、合成身份/审批/业务事实，无模型/网络业务调用、用户库 migration 或真实数据。日志仅替换工作区绝对路径与清除行尾空白，失败历史保留。

## 边界与后续

本卡交付可从源码/发行包显式调用的本地合成参考模式及持久结果对账，不是生产网络接收端。同步 SQLite COMMIT 不承诺可被 asyncio timeout 抢占；驱动接收 COMMIT 后的外部撤权/到期不保证追溯撤销。接收端历史丢失不能推断效果未发生，也不能授权重新派发。真实目标认证、远程原子接纳、其他领域 fencing、Graph/审批事件恢复、worker 与可靠通知继续待办；T03/Core 与 PostgreSQL S2 不关闭。V0.5-T07 保持 29/80。

Live、Enterprise、Performance、真实飞书、PostgreSQL 本轮未运行；9 个外部标记用例按默认门禁不选中，不能计为通过。已推送的 75fdd64 不包含本卡源码，远程 CI 结论另列。

## 已推送基线的 CI

75fdd64b17da54a5d8961cc688e9380ac9cb55dc 的全部工作流已结束：[CI](https://github.com/PureBlueFrank/oria/actions/runs/43339497587) 核心测试、quality、package、eval-golden、两项 extras-smoke 成功；arm64-smoke 未执行步骤即取消，annotation 为 `The configuration 'macos-14-arm64-us-default' is not supported`。CI 总结 cancelled，不能记为全绿。[Pages](https://github.com/PureBlueFrank/oria/actions/runs/43339497264) 成功。本卡源码未推送，不属于上述远程测试范围。

## 最终本地结果

完整 [community.log](community.log)：**1621 passed / 1 failed / 9 deselected / 5 warnings**，542.17 秒。唯一失败在既有 test_configured_receiver_survives_process_restart_and_revocation 的 listener.bind((127.0.0.1, 0))，沙箱拒绝端口；允许本机回环后 [loopback-retry.log](loopback-retry.log) **1 passed**，10.13 秒。保留首轮失败，不把完整运行改写为零失败，不重跑全套挑选结果。

随后仅新增一个源码服务独立进程恢复用例（源码未变）：[fresh-process.log](fresh-process.log) **1 passed / 14 deselected**，5.35 秒。用例先提交接收端结果并模拟丢失响应，再由独立 Python 进程重新装配 DatabaseResources、Runtime、身份目录、JobLeaseService 和源码 local_reference 服务；reconcile 将持久回执投影到原业务库，效果记录仅一行、attempt=1。这里 14 deselected 是该文件本次未选中的其他用例，已在先前定向/全量通过，不是外部 marker。

本轮共新增 15 项测试；最终 1623 个本地选中用例分别有通过证据，但没有声明最终测试文件在一次全量运行中全部零失败。[lint-complete.log](lint-complete.log) 为追加用例后的全库静态检查，405 文件格式/Ruff、198 源文件 mypy 通过。源码指纹与构建/完整测试执行时一致，测试文件指纹对应追加后的最终版本。

构建、smoke、wheel 字节一致性及 wheel 导入均通过。6 份受影响文档本地链接与 git diff --check 通过。生产接入、实时模型与跨平台 CI 不由这些本地结果替代。
