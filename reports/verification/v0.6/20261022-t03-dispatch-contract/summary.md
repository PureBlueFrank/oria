---
task_id: V0.6-T03
date: 2026-10-22
levels: [Fixture, Contract, Security, Recovery]
result: passed
scope: typed dispatch contract and in-memory conformance fixture only
source: uncommitted working tree based on 476b034
---

# 外部派发接纳与未知结果对账契约

新增 `src/oria/core/dispatch.py`：稳定 operation 绑定、接纳/查询结果、泛型 Adapter Protocol、当前权威 lease/参数/历史校验和纯结果投影。去重键不含 Job、epoch 或 payload hash；同键换内容冲突，接管复用原历史。pending 不等于完成，unknown/not_found 不授予重投；只有完整绑定的确定 receipt 才能映射为 succeeded/failed。

当前没有生产接收端、持久接纳/对账或真实网络调用。现有 `JobCouponDispatchService` 的精确内存 Adapter 限制保持。接收端原子租约权威与幂等效果边界的要求见 [ADR-040](../../../../docs/adr/ADR-040-fenced-dispatch-admission.md)；测试 receiver 只用内存锁和合成数据，不代表真实 Adapter、分布式 fencing 或 PostgreSQL S2。

## 验证

- `01-contract-initial.log`：uv 默认 cache 沙箱访问失败，未执行测试。后续使用项目已有 `.venv`，没有安装或更改业务依赖。
- `02-revalidation-red.log`：独立复审发现未重验复制值；8 个新增回归全部失败，复现无 receipt 的 unknown 被伪造成终态，以及嵌套 request/job/receipt 绕过。
- `03-targeted-green.log`：首次修复后契约 44 项与既有 Mock 派发 20 项，共 **64 passed**。保留此阶段结果，不当成最终严格重验的证明。
- `04-strict-revalidation-red.log`：3 个回归失败，复现 bytes 稳定键可被静默转换后产生第二次接纳，及 payload 类型转换不一致。
- `05-final-contract.log`：严格重验后 **47 passed / 3 warnings**，警告来自刻意复制的不合法合成值。
- `06-serializer-red.log`：将这 3 项序列化警告升级为错误，复现原始参数可能进入 warning。
- `07-final-contract.log`：校验前禁用序列化警告、保留严格嵌套校验，最终 **47 passed，无 warnings**。
- [独立复审](../20261022-parallel-progress/independent-review.md)：两项发现已修复，最终 11 项独立复验通过且无 warning。

静态检查、最终打包/源码指纹与组合 Community 覆盖见[本轮汇总](../20261022-parallel-progress/summary.md)。未运行 Live、Enterprise、Performance、新 PostgreSQL 或真实飞书；没有新依赖、migration、配置开关或默认执行入口。T02/T03/T04/Core 仍未关闭。
