# 执行轮次与失败历史

1. 初次调用锁定工具环境时，默认 uv 缓存位于沙箱不可写的用户缓存目录，未执行测试。改用明确可写的 `/private/tmp/oria-uv-cache`，保持 offline/no-sync；不修改依赖或锁文件。
2. 首轮定向 2 failed / 10 passed：真实回环测试被沙箱阻止监听端口；资源回收测试尝试修改已封存 RuntimeServices 实例的方法而被拒绝。前者改为经自动审批允许的本机回环验证；后者在类方法边界观测实际资源关闭，不修改已封存实例。此轮产生的异步生成器关闭提示来自测试封存错误后的资源未进入正常退出路径，后续实际构造失败测试已覆盖正常资源回收。
3. 允许真实回环后的首轮 1 failed / 11 passed，见 [lifecycle-first.log](lifecycle-first.log)。重启后测试重新生成签名时间戳，合法增加 replay 记录，原断言误把新投递等同于原始字节重试。改为固定原请求复验重试，并另测新 nonce 投递只增加 replay、决定审计与 outbox 不变。未改动业务去重逻辑。
4. 生命周期定向最终 12 passed / 1 warning，见 [lifecycle-final.log](lifecycle-final.log)。此后补充 HTTP/卡片共享决定用例，1 passed / 1 warning，见 [shared-services.log](shared-services.log)。最终全量套件包含全部 8 项新增测试。
5. 首次全项目格式检查发现新测试的一处多行断言格式，见 [lint-first.log](lint-first.log)。仅格式化该测试文件后，完整格式、Lint 和 mypy 均通过，见 [lint-final.log](lint-final.log)。

这些失败分别为执行环境、测试观测方法、测试断言和格式问题，未隐藏或改写成产品缺陷修复。`lifecycle-first.log` 仅移除行尾空白以满足差异检查，失败内容、计数与历史保留。未通过重复采样挑选最好结果，未调用真实模型或企业服务。
