# V0.6-T02 持久审批卡片引用

日期：2026-10-11，基于 main `6f37d60` 的本地增量。**T02 进行中**，未提交或推送。

2026-10-12 校正：本卡“重复解析不写审计”的结论仅来自未装配 AuditService 的替身，不能代表真实运行时。实际解析记录授权审计，但不决定审批或写 receipt/replay/outbox。原始验证历史保留，修复与真实审计验证见 [审查修复验证卡](../20261012-review-fixes/summary.md)。

## 交付

- `ApprovalCardBindings` 内部签发与解析服务：高熵不透明引用，只保存 SHA-256，绑定可信 tenant、approval ID/完整版本、issuer/subject/actor、chat/message ID 与有效期。
- `platform_0011` 新表、tenant 复合外键与 PostgreSQL FORCE RLS；同步包完整性清单、迁移验证 schema 和当前 head 断言。旧 receipt 迁移用例钉住 platform_0010，保留历史验证范围。
- 解析前后重查当前身份及既有审批 Policy，检查职责分离、pending 状态、有效期和版本。返回受信内部审批快照；不提交决定、消费按钮或恢复 Graph，重复解析不写审计。

## 验证

- 定向：22 passed、1 warning，8.26 秒，覆盖新 migration 升级/降级、真实 SQLite、连接重建、真实签名合成 Feishu 回调、重复解析无决定、错误会话/消息/tenant/actor/sender、未知引用、事件过期、撤权、旧版本、自批和缺失审批。
- 上下文为轻量替身，Policy/ApprovalService/Repository 和持久库为真实组件；不声明完整生产运行时/点击受理 E2E。只验证资源重建，不等同于进程崩溃恢复。
- 格式、Ruff、179 源文件 mypy 通过；首轮新代码/测试格式问题已修正。
- 初次构建因沙箱 DNS 获取 hatchling 失败，经批准联网复验成功；失败历史保留。
- 全量回归、wheel 安装与初始化最终结果待补齐。

## 边界

绑定只能由受信业务服务使用服务端确认的投递 chat/message ID 创建。生产投递/更新、带理由的拒绝交互、receipt 与条件决定同事务、真实 Job/wait 与 Graph 恢复仍待实现，默认 Feishu receiver 未打开。没有真实企业数据/消息、Live、Enterprise 或 Performance 运行，没有迁移用户数据库。真实 PostgreSQL 验证仍未运行；旧 T01/T07 状态不变。

指南见 [API 使用说明](../../../../docs/guides/api-foundation.md)，前置证据见 [审批启动装配](../20261008-t02-lifecycle/summary.md)。
