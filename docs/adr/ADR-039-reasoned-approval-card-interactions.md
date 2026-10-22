# ADR-039：带理由拒绝与审批卡片详情

- 状态：实施中，本地实现完成，新增真实飞书交互待验收
- 日期：2026-10-21
- 关联任务：V0.6-T02；沿用 ADR-037 的身份、事务与出站边界

## 决策

飞书拒绝复用现有 ApprovalService，不另设决定路径。受信服务为同一审批版本、身份、chat/message 和有效期原子签发 approve、reject、view 三种绑定；动作类型以数据库记录为准。卡片上的引用只含随机前缀和动作后缀，数据库只保存完整引用的 SHA-256。三种引用共享 256-bit 随机前缀，方便详情响应重建原有控件；知道同组引用不增加任何权限，每次点击仍须匹配服务端已签发记录并重新检查当前身份、Policy、职责分离和有效期。

拒绝使用 JSON 2.0 表单，输入字段固定为 `rejection_reason`，最大 1000 字，拒绝缺失、空白、超长、非字符串和未知字段。原始回调仍先完整验签、解密；规范化事件只携带该有界字段，理由不决定动作类型。理由纳入受理内容摘要，同一引用提交不同理由返回冲突，不覆盖原决定。正文仅按既有审批领域契约存储，receipt/replay、审计和 outbox 不保存理由原文。

批准或拒绝继续在 receiver 唯一事务中提交审批快照 CAS、receipt/replay、审批审计和 outbox。两个不同动作竞争同一审批时只有一个胜者。重复回调重查当前权限，但不重做决定；投影失败不撤销已提交决定。

详情使用绑定的 view 动作。客户端只可提供 1–20 的严格整数页码，不能提交审批 ID、租户、角色、checkpoint 或决定意图；页码同时参与去重键，并在当前授权投影的实际页数内验证。查看及翻页不写审批决定、成功审批审计或业务 outbox。重试复用原控件，不再签发绑定。审批版本变化后，详情可展示当前已授权状态，但不恢复旧版本决定按钮。

每页最多五段，使用现有 IMViewModel 和统一脱敏；JSON 2.0 的文本转义 Markdown 与 HTML 标记。带表单的占位、激活和终态始终使用 JSON 2.0，避免更新时切换卡片结构；既有 JSON 1.0 展示 golden 和单独签发的批准引用保持兼容。终态移除决定按钮和输入框，可保留只读详情入口。

## 迁移和验证边界

新增 `platform_0012`，不改写 `platform_0011`：扩展绑定意图约束，记录 `card_schema`，旧记录默认 1.0。保留 tenant 复合外键和 PostgreSQL FORCE RLS；PostgreSQL 使用原生 ALTER，SQLite 使用 Alembic batch。存在 reject/view 记录时降级失败关闭，不通过删除记录降级。API 不自动升级用户数据库。

本轮只迁移临时 SQLite 测试库。既有 PostgreSQL 17.11 和飞书批准的通过卡保持历史范围，不覆盖本 revision、JSON 2.0 表单或翻页。本地验证见[验证卡](../../reports/verification/v0.6/20261021-t02-approval-interactions/summary.md)；T02 不因本增量自动关闭，Job/wait/Graph、默认会话投递和可靠出站仍分别属于 T03–T05。

协议参考：[新版回调 SDK 模型](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/event/callback/model/p2_card_action_trigger.py)、[JSON 2.0 表单容器](https://open.feishu.cn/document/feishu-cards/card-json-v2-components/containers/form-container)、[输入框](https://open.feishu.cn/document/feishu-cards/card-json-v2-components/interactive-components/input)、[按钮](https://open.feishu.cn/document/feishu-cards/card-json-v2-components/interactive-components/button)。
