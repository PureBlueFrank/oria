# ADR-037：API 身份映射与飞书受理边界

- 状态：实施中，T02 尚未收口
- 日期：2026-10-03
- 关联任务：V0.6-T02 / T03 / T05

## 背景与决策

HTTP 与飞书入口需要把已验证外部身份转换为现有 Principal/Policy 契约。直接采用 token 的角色、将 webhook 验签等同于审批权限，或在数据库受理前返回成功，都会破坏已有权限和恢复边界。

本轮采用固定 RS256、受信 HTTPS JWKS 和服务端身份目录。目录按外部 issuer/subject 映射 human actor 与独立 service executor；租户、角色和属性全部由目录管理。启动时验证目录，每个请求/资源授权边界重新读取，撤权不依赖 bearer 过期。JWT 和飞书都使用同一个目录模型；重复外部标识或同一 Principal 的冲突属性会失败关闭。

资源访问使用现有 PolicyEngine，不建立第二套业务 RBAC。HTTP 身份验证成功只代表“谁在请求”，资源授权、审批职责分离及恢复时重新鉴权仍必需。2026-10-06 新增可注入运行时的审批详情/决定路由，复用 ApprovalService；Job 路由尚未交付；10 月 8 日补齐显式配置的 SQLite 审批生命周期装配。

飞书适配器验证时间窗、签名、Token、应用和平台身份，输出经过结构约束的消息或不透明卡片 action 引用。nonce/source message/action 稳定键随规范化结果传递；真正的防重放和去重必须与受理持久化在同一事务中完成。没有 durable receiver 时 HTTP 返回 503；不能用内存 set 或先写“已处理”标识替代业务受理。2026-10-05 已实现 `TransactionalFeishuReceiver`，由受信服务处理器在同一事务内重新鉴权并持久化；SQLite 合成受理可验证一次性语义。真实 Job/审批处理器仍未接通，T03 不因本基础实现自动满足依赖。

出站通道限制可信 tenant/chat/subject 白名单，执行现有通知策略并使用纯文本/版本化卡片投影。回执不确定时不自动重发；T05 才装配可靠投递。展示层不接受任意字典字段，每页最多五段，携带稳定详情编号；后续详情端点必须再次按当前用户授权。

2026-10-06 审批 HTTP 决定要求详情版本指纹；Repository 更新以完整读取快照为条件，决定与审计/outbox 一起提交，防止不同回调或客户端并发覆盖。指纹仅检测旧版本，不授予权限，也不替代后续持久 action/wait 绑定或 Graph 恢复授权。

2026-10-08：审批启动要求显式 YAML 配置，不继承个人配置或环境覆盖；只读 schema/head 检查先于运行时启动，不自动迁移。当前装配限定 Mock/Fixture 与 SQLite，身份撤销即时检查，新增身份/角色需要重启 Policy 快照。

2026-10-11：新增持久审批卡片引用，摘要存储，tenant/审批版本/服务端决定意图/外部身份/chat/message/有效期绑定；仅受信内部服务签发。同日后续接通 SQLite 事务化批准受理：receipt/replay、完整快照 CAS、审批审计与 outbox 由 receiver 的唯一 session 提交；新 action 重查 Policy/SOD/过期/冻结策略版本，已提交重试重查当前身份、Policy、SOD 与 binding，但不再要求旧 pending revision。当前只签发显式 `approve` 意图；没有安全理由契约的 reject 失败关闭。

## 局限与后续

2026-10-12 审查修复：受理提交前重查身份与回调有效期，覆盖异步处理期间撤权和重复回调；事务内 Policy 拒绝通过请求局部异常携带审计操作，receiver 回滚后才调用原有 AuditService，保持业务原子性并避免 SQLite 双连接写锁争用。解析会记录授权审计，业务决定/receipt/replay/outbox 仍由受理接口负责；真实审计装配纳入解析回归。审批失效化的独立事务与 session-bound 路径共用同一 SQL 实现。

2026-10-16 服务启动装配：新增严格布尔 `feishu_approval_receiver`，默认关闭，显式启用须同时配置 Feishu 与审批运行时。`APIServices` 将审批 HTTP 与 receiver 绑定到同一个运行时生命周期，不能与独立注入的审批/receiver 混用；保留既有库注入与审批专用 factory。只读 schema 门禁、Mock/Fixture/SQLite 限制、逐次撤权与事务受理契约不变。当前仅路由卡片 action，缺少 Job 处理器的文本消息在写账本前返回 503；不在启动时创建审批、签发绑定或发送消息。真实独立进程重启和回环 HTTP 已纳入验证，生产投递/更新、带理由拒绝及 Job/wait 恢复仍未实现。

- 文件目录是可替换的首个服务端身份源，需用受限部署权限与原子替换维护；不是完整的企业身份治理或 OIDC 登录流程。
- 2026-10-18 投递接缝：受信 `ApprovalCardDelivery` 先授权并发送无按钮共享卡片，取得回执后才签发持久引用，再 PATCH 同一消息激活批准按钮。发送人与审批人分别从当前目录解析，保持原有通知/审批 Policy、明确目的地白名单和职责分离；获取 token 后、外部写入前再次核验身份。目标守卫只允许当前实例确认发送的同 tenant/chat/主体共享卡片，最多 1024 条；不是持久投递账本，超时不重发，部分成功须对账。
- 已启用的审批 receiver 在唯一事务提交后，重新授权并投影当前终态，按新版回调结构返回 raw 卡片并移除按钮；重复和跨进程重投不新增成功审计、决定或 outbox。投影失败可以重试受理查询，但不会回滚已提交决定，503 不等于业务未提交。默认服务启动仍不发卡片，投递触发和 T05 可靠恢复未装配。
- 持久去重、审批 HTTP、卡片原子批准及受信投递/结果更新已有本地 SQLite 验证；不包含飞书详情/分页、带理由拒绝或 Job/wait 恢复。
- 飞书签名、加密和 PATCH/新版卡片响应按官方 SDK 源码核对。2026-10-18 独立 OriaTest 已完成真实投递、PATCH、重启后的签名加密按钮回调及终态显示确认；同一真实请求在内存中复验后未新增决定审计/outbox。这不是平台自身重投或 Job/Graph 恢复验收，详见 [审批联调指南](../guides/feishu-approval-validation.md)。ClawBot 与其他程序共用，不切换其现有长连接。
- 本次真实请求的签名时间戳为带时区、纳秒及可选单调时钟后缀的 Go `Time.String()` 文本，旧实现只接受 Unix 秒而返回 401。接收器现仅按明确格式解析 Unix 秒或该 Go 文本，按数字 UTC 偏移求墙上时间；完整原始字段仍参与签名，Go 文本也原样参与 replay 键，单调时钟不用于跨进程时效判断。时间窗仍至多 300 秒，支持原有更短配置；未知格式、非法日期/偏移、过期/未来和签名篡改仍拒绝。格式语义见 [Go 时间文档](https://pkg.go.dev/time#Time.String)，真实失败与修复证据见 [续验记录](../../reports/verification/v0.6/20261018-t02-real-approval/summary.md)。
- 新增 FastAPI、PyJWT、cryptography 与显式 uvicorn 依赖，经锁文件同步。10 月 5 日新增 `platform_0010` 账本迁移及完整性清单，保留 tenant 隔离与 PostgreSQL FORCE RLS；仅测试临时库，无生产迁移或新环境变量。回执不存储 raw body、正文、角色、凭证或卡片 token；到期清理不在本轮自动启用。

## 验证

2026-10-21：带理由拒绝和逐次授权详情/翻页已接入同一受理事务，卡片表单使用 JSON 2.0，旧批准引用保持兼容。新增迁移及交互边界见 [ADR-039](ADR-039-reasoned-approval-card-interactions.md)。此前仅 approve 的描述保留为历史实现范围；新增真实表单/分页、本 revision 的 PostgreSQL 与 T02 总门禁仍待验收。

见 [API 使用与限制](../guides/api-foundation.md) 和 [验证卡](../../reports/verification/v0.6/20261003-t02-foundation/summary.md)。现有失败/暂停 Live 卡保持不变，V0.6 Core 未完成。
